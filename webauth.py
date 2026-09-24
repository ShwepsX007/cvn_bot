"""
Проверка данных от виджета "Log in with Telegram" на сайте.
https://core.telegram.org/widgets/login

ВАЖНО: чтобы виджет вообще заработал, доменное имя сайта нужно ОДИН РАЗ
привязать к боту через @BotFather -> /setdomain -> выбрать бота -> указать
https://ваш-домен (например https://amneziawg.fun). Без этого Telegram
откажется показывать окно авторизации ("Bot domain invalid").
"""

import hashlib
import hmac
import time


def verify_telegram_auth(data: dict, bot_token: str, max_age_seconds: int = 86400) -> bool:
    """
    Проверяет подпись данных, присланных Telegram Login Widget.
    data - словарь параметров (id, first_name, last_name, username, photo_url, auth_date, hash, ...)
    Возвращает True, только если подпись верна и данные не старше max_age_seconds.
    """
    if not data or "hash" not in data or "id" not in data or "auth_date" not in data:
        return False

    received_hash = data.get("hash")
    check_data = {k: v for k, v in data.items() if k != "hash" and v is not None and v != ""}

    data_check_string = "\n".join(f"{k}={check_data[k]}" for k in sorted(check_data.keys()))
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, str(received_hash)):
        return False

    try:
        auth_date = int(data.get("auth_date"))
    except (TypeError, ValueError):
        return False

    if time.time() - auth_date > max_age_seconds:
        return False

    return True
