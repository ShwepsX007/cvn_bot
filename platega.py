"""
Интеграция с платежным агрегатором Platega (https://my.platega.io — личный кабинет).

Документация: https://docs.platega.io (и wiki.platega.io)

Интерфейс намеренно повторяет aipay.py, чтобы бизнес-логика (web_app.py / user_handlers.py)
не зависела от конкретного провайдера. Переключение провайдера - через модуль payments.py
и настройку payment_provider в админ-панели.

Аутентификация: заголовки X-MerchantId (UUID мерчанта) и X-Secret (API-ключ).
Выдает менеджер при подключении, также видны в ЛК Platega на странице "Настройки".

Основные методы (по документации):
- POST {BASE_URL}/transaction/process -> создание платежа, ответ содержит redirect (ссылка на оплату) и transactionId
- GET  {BASE_URL}/transaction/{id}    -> проверка статуса (статус строкой: PENDING / CONFIRMED / CANCELED / CHARGEBACKED)
- Callback: Platega сам шлет POST на URL из ЛК (Настройки -> Callback URLs) с теми же
  заголовками X-MerchantId / X-Secret и телом {"id","amount","currency","status",...}.
  Наш приемник: /webhook/platega в web_app.py.

Что стоит подтвердить у менеджера при подключении (помечено TODO-MANAGER):
1. Хост API (по wiki это api.platega.io; встречается также app.platega.io).
2. Значение paymentMethod для нужного способа оплаты (в примерах доков: 2 = СБП QR, 13 = крипта).
3. Требуется ли поле metadata.userId для вашего магазина (мы передаем всегда - так безопаснее).
"""

import hmac
import aiohttp
import database as db

BASE_URL = "https://api.platega.io"  # TODO-MANAGER: подтвердить хост API

# Унифицированные коды статусов (совпадают с aipay.py), чтобы внешний код не менять
STATUS_PENDING = 1
STATUS_METHOD_SELECTED = 2
STATUS_SUCCESS = 3
STATUS_ERROR = 4
STATUS_CANCELLED = 5
STATUS_ON_HOLD = 6

# Сырой строковый статус Platega -> наш код
_STATUS_MAP = {
    "CONFIRMED": STATUS_SUCCESS,
    "SUCCESS": STATUS_SUCCESS,
    "PAID": STATUS_SUCCESS,
    "CANCELED": STATUS_CANCELLED,
    "CANCELLED": STATUS_CANCELLED,
    "EXPIRED": STATUS_CANCELLED,
    "CHARGEBACKED": STATUS_ERROR,
    "FAILED": STATUS_ERROR,
    "ERROR": STATUS_ERROR,
    "DECLINED": STATUS_ERROR,
}


class PlategaError(Exception):
    pass


def map_status(raw_status) -> int:
    """Строковый статус Platega -> унифицированный код (STATUS_*)."""
    return _STATUS_MAP.get(str(raw_status or "").upper(), STATUS_PENDING)


def _get_creds() -> tuple[str, str]:
    merchant_id = db.get_setting("platega_merchant_id")
    secret = db.get_setting("platega_secret")
    if not merchant_id or not secret:
        raise PlategaError(
            "Platega не настроена. Задайте Merchant ID и Secret в "
            "Админ-панели → ⚙️ Настройки бота (выдает менеджер Platega)."
        )
    return merchant_id, secret


def _headers() -> dict:
    merchant_id, secret = _get_creds()
    return {
        "X-MerchantId": merchant_id,
        "X-Secret": secret,
        "Content-Type": "application/json",
    }


def _get_payment_method() -> int:
    """ID способа оплаты. TODO-MANAGER: уточнить нужный (в примерах доков: 2 = СБП QR)."""
    raw = db.get_setting("platega_payment_method") or "2"
    try:
        return int(raw)
    except ValueError:
        return 2


async def create_order(amount, currency: str = "RUB", tg_id=None, username=None,
                       client_ip=None, description=None) -> tuple[str, str]:
    """Создает платеж в Platega. Возвращает (payment_url, uid) - как aipay.create_order."""
    url = f"{BASE_URL}/transaction/process"
    site_url = (db.get_setting("site_url") or "https://amneziawg.fun").rstrip("/")

    payload = {
        "paymentMethod": _get_payment_method(),
        "paymentDetails": {
            "amount": round(float(amount), 2),
            "currency": currency,
        },
        "description": description or f"Доступ к сервису AmneziaWG (пользователь {tg_id})",
        "return": f"{site_url}/dashboard",
        "failedUrl": f"{site_url}/dashboard",
        "payload": f"vpn_tg{tg_id}" if tg_id else None,
    }
    # Метаданные для антифрода (для части магазинов обязательны - передаем всегда)
    metadata = {}
    if tg_id:
        metadata["userId"] = str(tg_id)
    if username:
        metadata["userName"] = f"@{username}" if not str(username).startswith("@") else str(username)
    if client_ip:
        metadata["clientIp"] = client_ip
    if metadata:
        payload["metadata"] = metadata
    # Не отправляем None-поля
    payload = {k: v for k, v in payload.items() if v is not None}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, headers=_headers(), timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                data = await resp.json(content_type=None)
    except PlategaError:
        raise
    except Exception as e:
        raise PlategaError(f"Не удалось связаться с Platega: {e}")

    if not isinstance(data, dict) or "redirect" not in data or "transactionId" not in data:
        raise PlategaError(f"Некорректный ответ Platega при создании платежа: {data}")

    return data["redirect"], data["transactionId"]


async def get_order_status(uid: str) -> dict:
    """Статус платежа в виде {'id': <наш код STATUS_*>, 'name': <сырой статус>}."""
    url = f"{BASE_URL}/transaction/{uid}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=_headers(), timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                data = await resp.json(content_type=None)
    except PlategaError:
        raise
    except Exception as e:
        raise PlategaError(f"Не удалось связаться с Platega: {e}")

    if not isinstance(data, dict) or "status" not in data:
        raise PlategaError(f"Некорректный ответ Platega при проверке статуса: {data}")

    raw = data.get("status")
    return {"id": map_status(raw), "name": raw}


def verify_webhook_headers(headers) -> bool:
    """
    Аутентификация callback от Platega: заголовки X-MerchantId и X-Secret
    должны совпасть с нашими настройками (по документации callback подписывается именно так).
    """
    merchant_id = db.get_setting("platega_merchant_id")
    secret = db.get_setting("platega_secret")
    if not merchant_id or not secret:
        return False
    got_mid = headers.get("X-MerchantId") or headers.get("x-merchantid") or ""
    got_secret = headers.get("X-Secret") or headers.get("x-secret") or ""
    return (
        hmac.compare_digest(str(got_mid), str(merchant_id))
        and hmac.compare_digest(str(got_secret), str(secret))
    )
