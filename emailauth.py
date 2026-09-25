"""
Email-аутентификация для сайта и бота AmneziaWG VPN.

Модель (как в LiqScope, но в обе стороны):
  - регистрация на сайте по почте: email + пароль (scrypt в stdlib), письмо с ссылкой
    подтверждения (24 ч); вход по email+паролю;
  - привязка почты из Telegram-бота: пользователь вводит email в боте, получает письмо
    со ссылкой-токеном (2 ч), переходит - и почта привязана к его tg_id;
  - привязка с сайта (уже залогинен через Telegram-виджет): форма в кабинете, письмо
    со ссылкой-привязкой;
  - вход через Telegram-виджет при наличии email-сессии автоматически довязывает tg.

Аккаунты и токены - в таблицах email_accounts / email_tokens (database.py).
Без внешних зависимостей: scrypt/hmac/secrets из стандартной библиотеки.
"""

import hashlib
import hmac
import re
import secrets
import time
from datetime import datetime

import database as db

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24}$")
PASSWORD_MIN = 8
PASSWORD_MAX = 200
BAD_PASSWORDS = {"password", "passw0rd", "12345678", "123456789", "1234567890",
                 "qwertyui", "qwerty123", "11111111", "пароль123", "пароль1234"}
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1

TOKEN_TTL = {
    "verify": 24 * 3600,   # подтверждение почты - сутки
    "reset": 3600,         # сброс пароля - час
    "link": 2 * 3600,      # привязка почты к tg - два часа
}


def normalize_email(raw) -> str:
    return str(raw or "").strip().lower()


def valid_email(raw) -> bool:
    email = normalize_email(raw)
    if not email or len(email) > 254 or ".." in email:
        return False
    return bool(EMAIL_RE.match(email))


def password_problem(password, email: str = "") -> str:
    """Пустая строка - пароль годный, иначе код проблемы: short / long / weak."""
    p = str(password or "")
    if len(p) < PASSWORD_MIN:
        return "short"
    if len(p) > PASSWORD_MAX:
        return "long"
    if p.lower() in BAD_PASSWORDS or (email and p.lower() == normalize_email(email)):
        return "weak"
    return ""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(str(password).encode("utf-8"), salt=salt,
                        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not password or not stored:
        return False
    try:
        kind, n, r, p, salt_hex, hash_hex = str(stored).split("$")
        if kind != "scrypt":
            return False
        dk = hashlib.scrypt(str(password).encode("utf-8"), salt=bytes.fromhex(salt_hex),
                            n=int(n), r=int(r), p=int(p), dklen=len(hash_hex) // 2)
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError, MemoryError):
        return False


def issue_token(email: str, kind: str, tg_id=None) -> str:
    ttl = TOKEN_TTL[kind]
    return db.create_email_token(normalize_email(email), kind, tg_id, ttl)


def peek_token(token: str, kind: str):
    """(email, tg_id) если токен жив, иначе None. Токен НЕ расходуется."""
    return db.peek_email_token(token, kind)


def use_token(token: str, kind: str):
    """(email, tg_id) если токен жив, иначе None. Токен расходуется одноразово."""
    return db.consume_email_token(token, kind)


class RateBucket:
    """Простейший in-memory лимитер: N попыток за окно на ключ (обычно IP).
    Хватает для антибрутфорса логина/регистрации; при рестарте счетчики обнуляются."""

    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self._hits = {}

    def allow(self, key: str) -> bool:
        now = time.time()
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        self._hits[key] = hits
        return True


LOGIN_LIMITER = RateBucket(8, 600)        # вход по почте: 8 попыток за 10 минут
REGISTER_LIMITER = RateBucket(5, 3600)    # регистрация: 5 писем в час с IP
MAIL_LINK_LIMITER = RateBucket(5, 3600)   # повторная отправка ссылок-привязок
