"""Единая точка входа автоматических платежей для сайта и Telegram-бота.

payment_provider: off / aipay / platega. Новый заказ использует выбранного
провайдера; ранее созданный проверяется у провайдера, сохранённого в заказе,
даже после отключения автооплаты или переключения платёжной системы.
"""

import aipay
import platega
import database as db

STATUS_PENDING = 1
STATUS_METHOD_SELECTED = 2
STATUS_SUCCESS = 3
STATUS_ERROR = 4
STATUS_CANCELLED = 5
STATUS_ON_HOLD = 6

_PROVIDERS = {"aipay": aipay, "platega": platega}
PROVIDER_LABELS = {"off": "Выключена", "platega": "Platega", "aipay": "AiPay"}
SETTING_KEYS = {"payment_provider", "aipay_api_key", *platega.SETTING_KEYS}


class AutoPayDisabled(Exception):
    pass


class PaymentConfigurationError(ValueError):
    pass


def get_provider() -> str:
    return (db.get_setting("payment_provider") or "off").strip().lower()


def normalize_setting(key: str, value: str) -> str:
    value = (value or "").strip()
    if key == "payment_provider":
        value = value.lower()
        if value not in PROVIDER_LABELS:
            raise PaymentConfigurationError("Выберите провайдера: off, platega или aipay.")
    if key in platega.SETTING_KEYS:
        return platega.normalize_setting(key, value)
    return value


def configuration_errors(provider: str | None = None, settings: dict | None = None) -> list[str]:
    provider = get_provider() if provider is None else provider
    if provider == "off":
        return []
    if provider == "platega":
        return platega.configuration_errors(settings)
    if provider == "aipay":
        key = (settings or {}).get("aipay_api_key", db.get_setting("aipay_api_key"))
        return [] if key and key.strip() else ["Сначала задайте API-ключ AiPay."]
    return ["Неизвестный провайдер автооплаты. Выберите off, platega или aipay."]


def validate_configuration(provider: str | None = None, settings: dict | None = None):
    errors = configuration_errors(provider, settings)
    if errors:
        raise PaymentConfigurationError(" ".join(errors))


def is_auto_pay_enabled() -> bool:
    provider = get_provider()
    return provider in _PROVIDERS and not configuration_errors(provider)


def module(provider: str | None = None):
    return _PROVIDERS.get(get_provider() if provider is None else provider)


async def create_order(amount, currency: str = "RUB", tg_id=None, username=None,
                       client_ip=None, provider: str | None = None):
    """Создание заказа; provider позволяет зафиксировать выбор до await."""
    provider = get_provider() if provider is None else provider
    mod = module(provider)
    if mod is None:
        raise AutoPayDisabled("Автоматическая оплата временно отключена.")
    validate_configuration(provider)
    if mod is aipay:
        return await aipay.create_order(amount, currency)
    return await mod.create_order(amount, currency, tg_id=tg_id, username=username, client_ip=client_ip)


async def get_order_status(uid: str, provider: str | None = None, expected_amount=None) -> dict:
    """Проверяет старый заказ независимо от текущего переключателя автооплаты."""
    mod = module(provider)
    if mod is None:
        raise AutoPayDisabled("Автоматическая оплата временно отключена.")
    if mod is platega:
        return await platega.get_order_status(uid, expected_amount=expected_amount)
    return await mod.get_order_status(uid)
