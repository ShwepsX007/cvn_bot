"""
Интеграция с платежной системой AiPay (https://lks.aipay.onl — личный кабинет).

Документация (из скриншотов кабинета):
- POST {BASE_URL}/api/v2/order/create   -> создание заказа, возвращает payment_url + uid
- GET  {BASE_URL}/api/v2/order/status/:uid -> получение текущего статуса заказа
- Вебхук: AiPay сам шлет POST на URL, указанный в разделе "Сведения о магазине" личного кабинета.
  Тело: {"uid": "...", "signature": "...", "status": {"id": N, "name": "..."}}
  Подпись = md5(uid + ':' + api_key)

Каталог статусов:
  1 - Pending Select Payment Method (ожидает выбора способа оплаты)
  2 - Payment method selected (способ оплаты выбран)
  3 - Success (успешно, деньги получены)
  4 - Error (ошибка)
  5 - Cancelled (отменен)
  6 - On Hold (на удержании)
"""

import hashlib
import aiohttp
import database as db

BASE_URL = "https://ads.aipay.onl"

STATUS_PENDING = 1
STATUS_METHOD_SELECTED = 2
STATUS_SUCCESS = 3
STATUS_ERROR = 4
STATUS_CANCELLED = 5
STATUS_ON_HOLD = 6


class AiPayError(Exception):
    pass


def _get_api_key() -> str:
    key = db.get_setting("aipay_api_key")
    if not key:
        raise AiPayError(
            "API-ключ AiPay не задан. Задайте его в Админ-панели → ⚙️ Настройки бота → 🔑 API-ключ AiPay."
        )
    return key


async def create_order(amount, currency: str = "RUB") -> tuple[str, str]:
    """Создает заказ в AiPay. Возвращает кортеж (payment_url, uid)."""
    api_key = _get_api_key()
    url = f"{BASE_URL}/api/v2/order/create"
    headers = {"Content-Type": "application/json", "api-key": api_key}
    payload = {"amount": round(float(amount), 2), "currency": currency}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                data = await resp.json(content_type=None)
    except Exception as e:
        raise AiPayError(f"Не удалось связаться с AiPay: {e}")

    if not isinstance(data, dict) or "payment_url" not in data or "uid" not in data:
        raise AiPayError(f"Некорректный ответ AiPay при создании заказа: {data}")

    return data["payment_url"], data["uid"]


async def get_order_status(uid: str) -> dict:
    """Возвращает статус заказа в виде {'id': int, 'name': str}."""
    api_key = _get_api_key()
    url = f"{BASE_URL}/api/v2/order/status/{uid}"
    headers = {"api-key": api_key}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                data = await resp.json(content_type=None)
    except Exception as e:
        raise AiPayError(f"Не удалось связаться с AiPay: {e}")

    if not isinstance(data, dict) or "status" not in data:
        raise AiPayError(f"Некорректный ответ AiPay при проверке статуса: {data}")

    return data["status"] or {}


def verify_webhook_signature(uid: str, signature: str) -> bool:
    """Проверяет подпись вебхука: signature == md5(uid + ':' + api_key)."""
    api_key = db.get_setting("aipay_api_key")
    if not api_key or not uid or not signature:
        return False
    expected = hashlib.md5(f"{uid}:{api_key}".encode()).hexdigest()
    return expected == signature
