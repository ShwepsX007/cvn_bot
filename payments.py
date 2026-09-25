"""
Единая точка входа для автоматических платежей.

Весь остальной код (web_app.py, user_handlers.py) работает ТОЛЬКО с этим модулем
и ничего не знает про конкретного провайдера. Провайдер переключается настройкой
payment_provider в базе (Админ-панель → ⚙️ Настройки бота → 💳 Автооплата):

  off     - автооплата выключена (кнопки скрыты на сайте и в боте; ручная оплата работает)
  aipay   - старый провайдер AiPay   (модуль aipay.py)
  platega - новый провайдер Platega  (модуль platega.py)

Коды статусов унифицированы у всех провайдеров (см. aipay.py / platega.py).
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

_PROVIDERS = {
    "aipay": aipay,
    "platega": platega,
}


class AutoPayDisabled(Exception):
    pass


def get_provider() -> str:
    return (db.get_setting("payment_provider") or "off").strip().lower()


def is_auto_pay_enabled() -> bool:
    return get_provider() in _PROVIDERS


def module():
    """Активный платежный модуль или None, если автооплата выключена."""
    return _PROVIDERS.get(get_provider())


async def create_order(amount, currency: str = "RUB", tg_id=None, username=None, client_ip=None):
    """Создание платежа у активного провайдера -> (payment_url, uid)."""
    mod = module()
    if mod is None:
        raise AutoPayDisabled("Автоматическая оплата временно отключена.")
    if mod is aipay:
        # Старый модуль не принимает доп. параметры метаданных
        return await aipay.create_order(amount, currency)
    return await mod.create_order(amount, currency, tg_id=tg_id, username=username, client_ip=client_ip)


async def get_order_status(uid: str) -> dict:
    """Статус заказа у активного провайдера -> {'id': STATUS_*, 'name': str}."""
    mod = module()
    if mod is None:
        raise AutoPayDisabled("Автоматическая оплата временно отключена.")
    return await mod.get_order_status(uid)
