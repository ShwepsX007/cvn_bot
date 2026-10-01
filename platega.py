"""Platega API: платёжные ссылки, статусы и проверка callback.

Документация: https://docs.platega.io/
Авторизация: X-MerchantId и X-Secret из личного кабинета / у менеджера.
Метод «auto» использует /v2/transaction/process (выбор способа на стороне Platega),
числовой метод — /transaction/process. Callback принимает /webhook/platega.
"""

import asyncio
import hmac
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlsplit
from uuid import UUID

import aiohttp
import database as db

BASE_URL = "https://app.platega.io"
DEFAULT_PAYMENT_METHOD = "auto"
SETTING_KEYS = ("platega_merchant_id", "platega_secret", "platega_payment_method", "site_url")

# Унифицированные коды статусов (как у AiPay).
STATUS_PENDING = 1
STATUS_METHOD_SELECTED = 2
STATUS_SUCCESS = 3
STATUS_ERROR = 4
STATUS_CANCELLED = 5
STATUS_ON_HOLD = 6

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
    """Неизвестный статус не подтверждает оплату."""
    return _STATUS_MAP.get(str(raw_status or "").strip().upper(), STATUS_PENDING)


def normalize_setting(key: str, value: str) -> str:
    """Проверка настроек для обеих админок. Ошибки не содержат введённые ключи."""
    value = (value or "").strip()
    if key == "platega_merchant_id":
        try:
            return str(UUID(value))
        except (ValueError, AttributeError):
            raise ValueError("Platega Merchant ID должен быть UUID из личного кабинета.") from None
    if key == "platega_secret":
        if not value or not value.isascii() or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in value):
            raise ValueError("Введите Platega Secret (API-ключ) без пробелов и переносов строк.")
        return value
    if key == "platega_payment_method":
        if value.lower() == "auto":
            return "auto"
        if not value.isascii() or not value.isdigit() or len(value) > 10 or not 0 < int(value) <= 2**31 - 1:
            raise ValueError("Метод Platega: auto (выбор на платёжной странице) или положительный ID, например 2 для СБП.")
        return str(int(value))
    if key == "site_url":
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme == "https" and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None
                and parsed.path in ("", "/") and "?" not in value and "#" not in value
                and "\\" not in value and not any(c.isspace() or ord(c) < 32 for c in value)
            )
            if parsed.port is not None and parsed.port == 0:
                valid = False
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Адрес сайта: https://ваш-домен без пути, параметров и данных авторизации.")
        return value.rstrip("/")
    return value


def configuration_errors(settings: dict | None = None) -> list[str]:
    values = {key: db.get_setting(key) or "" for key in SETTING_KEYS}
    if settings:
        values.update(settings)
    # Для баз без этого ключа сохраняем работоспособный дефолт, но некорректный
    # явно заданный метод не подменяем молча на другой.
    if not values["platega_payment_method"]:
        values["platega_payment_method"] = DEFAULT_PAYMENT_METHOD
    errors = []
    for key in SETTING_KEYS:
        try:
            normalize_setting(key, values[key])
        except ValueError as exc:
            errors.append(str(exc))
    return errors


def get_webhook_url(site_url: str | None = None) -> str:
    base = normalize_setting("site_url", site_url if site_url is not None else db.get_setting("site_url"))
    return f"{base}/webhook/platega"


def _get_creds() -> tuple[str, str]:
    try:
        return (
            normalize_setting("platega_merchant_id", db.get_setting("platega_merchant_id")),
            normalize_setting("platega_secret", db.get_setting("platega_secret")),
        )
    except ValueError as exc:
        raise PlategaError(f"Platega не настроена: {exc} Откройте Админ-панель → Настройки → Platega.") from None


def _headers() -> dict:
    merchant_id, secret = _get_creds()
    return {
        "X-MerchantId": merchant_id,
        "X-Secret": secret,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _get_payment_method() -> int | None:
    try:
        method = normalize_setting("platega_payment_method", db.get_setting("platega_payment_method") or DEFAULT_PAYMENT_METHOD)
    except ValueError as exc:
        raise PlategaError(str(exc)) from None
    return None if method == "auto" else int(method)


async def _request(method: str, path: str, payload: dict | None = None) -> dict:
    headers = _headers()
    try:
        async with aiohttp.ClientSession() as session:
            # Не перенаправляем запросы с секретными заголовками на другой хост.
            async with session.request(
                method, f"{BASE_URL}{path}", json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=15), allow_redirects=False,
            ) as resp:
                if not 200 <= resp.status < 300:
                    if resp.status in (401, 403):
                        raise PlategaError("Platega отклонила Merchant ID или Secret. Проверьте настройки магазина.")
                    if resp.status == 400:
                        raise PlategaError("Platega отклонила параметры платежа (HTTP 400). Проверьте доступность метода и лимиты суммы у менеджера.")
                    raise PlategaError(f"Platega вернула HTTP {resp.status}. Попробуйте позже или обратитесь в поддержку.")
                try:
                    data = await resp.json(content_type=None)
                except (ValueError, UnicodeDecodeError):
                    raise PlategaError("Platega вернула некорректный JSON.") from None
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise PlategaError("Не удалось связаться с Platega. Попробуйте позже.") from None
    if not isinstance(data, dict):
        raise PlategaError("Platega вернула некорректный ответ.")
    return data


async def create_order(amount, currency: str = "RUB", tg_id=None, username=None,
                       client_ip=None, description=None) -> tuple[str, str]:
    """Возвращает (payment_url, transaction_id). Не повторяет POST при таймауте."""
    try:
        if isinstance(amount, bool):
            raise ValueError
        decimal_amount = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if not decimal_amount.is_finite() or decimal_amount <= 0:
            raise ValueError
    except (InvalidOperation, ValueError):
        raise PlategaError("Сумма платежа должна быть положительным числом.") from None

    method = _get_payment_method()
    try:
        site_url = normalize_setting("site_url", db.get_setting("site_url"))
    except ValueError as exc:
        raise PlategaError(str(exc)) from None
    payload = {
        "paymentDetails": {"amount": float(decimal_amount), "currency": currency},
        "description": description or f"Доступ к сервису AmneziaWG (пользователь {tg_id})",
        "return": f"{site_url}/dashboard",
        "failedUrl": f"{site_url}/dashboard",
    }
    if method is not None:
        payload["paymentMethod"] = method
    if tg_id is not None:
        payload["payload"] = f"vpn_tg{tg_id}"
        # В том числе для почтового аккаунта с внутренним виртуальным ID.
        payload["metadata"] = {
            "userId": str(tg_id),
            "userName": f"@{str(username).lstrip('@')}" if username else str(tg_id),
        }
        if client_ip:
            payload["metadata"]["clientIp"] = client_ip

    path = "/v2/transaction/process" if method is None else "/transaction/process"
    data = await _request("POST", path, payload)
    payment_url = data.get("url") or data.get("redirect")
    uid = data.get("transactionId")
    try:
        parsed = urlsplit(payment_url) if isinstance(payment_url, str) else None
        if not parsed or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        if parsed.port == 0:
            raise ValueError
        if any(c.isspace() or ord(c) < 32 for c in payment_url) or not isinstance(uid, str):
            raise ValueError
        uid = str(UUID(uid))
    except ValueError:
        raise PlategaError("В ответе Platega отсутствует корректная ссылка на оплату или ID транзакции.") from None
    return payment_url, uid


def payment_matches(amount, currency, expected_amount) -> bool:
    """Проверка суммы и валюты до выдачи доступа. Без float-сравнения денег."""
    try:
        if isinstance(amount, bool):
            return False
        actual, expected = Decimal(str(amount)), Decimal(str(expected_amount))
        return actual.is_finite() and actual > 0 and actual == expected and currency == "RUB"
    except (InvalidOperation, ValueError):
        return False


async def get_order_status(uid: str, expected_amount=None) -> dict:
    try:
        uid = str(UUID(uid))
    except (ValueError, AttributeError):
        raise PlategaError("Некорректный ID транзакции Platega.") from None
    data = await _request("GET", f"/transaction/{uid}")
    raw = data.get("status")
    if not isinstance(raw, str) or not raw.strip():
        raise PlategaError("В ответе Platega отсутствует статус платежа.")
    status = map_status(raw)
    if status == STATUS_SUCCESS and expected_amount is not None:
        details = data.get("paymentDetails")
        if not isinstance(details, dict) or not payment_matches(details.get("amount"), details.get("currency"), expected_amount):
            raise PlategaError("Сумма или валюта платежа Platega не совпадает с заказом.")
    return {"id": status, "name": raw}


def verify_webhook_headers(headers) -> bool:
    """Callback авторизован только с нашими X-MerchantId и X-Secret."""
    try:
        merchant_id, secret = _get_creds()
    except PlategaError:
        return False
    got_mid = headers.get("X-MerchantId") or headers.get("x-merchantid") or ""
    got_secret = headers.get("X-Secret") or headers.get("x-secret") or ""
    # Сравниваем байты: даже некорректные Unicode-заголовки не вызывают TypeError.
    return (
        hmac.compare_digest(str(got_mid).encode(), merchant_id.encode())
        and hmac.compare_digest(str(got_secret).encode(), secret.encode())
    )
