"""
Отправка писем AmneziaWG VPN (подтверждение почты, сброс пароля, привязка к Telegram).

Два транспорта (переключаются настройкой mail_mode в базе, задается в /admin):

  smtp   - обычный SMTP через стандартный smtplib (Yandex, Mail.ru, Gmail с паролем
           приложения и любой другой). Настройки: mail_smtp_host/port/user/password/tls/from.
  resend - HTTPS API сервиса Resend (https://resend.com). Настройки: mail_resend_key, mail_from.
           Противопоказан, когда хостинг блокирует исходящие SMTP-порты (обычная история на VPS).
  auto   - Resend, если задан ключ, иначе SMTP, если задан хост.
  off    - письма не отправляются (функции честно возвращают ошибку; регистрация по почте
           при этом работать не будет - у пользователей нет способа получить письмо).

Все функции отправки БЛОКИРУЮЩИЕ (smtplib/urllib) - из асинхронного кода вызывайте
через asyncio.to_thread(), чтобы не морозить бота и сайт.
"""

import json
import smtplib
import urllib.request
import urllib.error
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

import database as db

BRAND = "AmneziaWG VPN"


def _s(key, default=""):
    val = db.get_setting(key)
    return val if val not in (None, "") else default


def mail_mode() -> str:
    mode = (_s("mail_mode", "auto") or "auto").strip().lower()
    if mode == "off":
        return "off"
    if mode in ("smtp", "resend"):
        return mode
    # auto
    if _s("mail_resend_key"):
        return "resend"
    if _s("mail_smtp_host"):
        return "smtp"
    return "off"


def is_configured() -> bool:
    return mail_mode() != "off"


def describe() -> str:
    mode = mail_mode()
    if mode == "resend":
        return f"Resend API ({_s('mail_from', 'from не задан')})"
    if mode == "smtp":
        return f"SMTP {_s('mail_smtp_host')}:{_s('mail_smtp_port', '587')} ({_s('mail_from', 'from не задан')})"
    return "не настроена"


def html_to_text(html: str) -> str:
    import re
    text = re.sub(r"<\s*br\s*/?>", "\n", html, flags=re.I)
    text = re.sub(r"</\s*(p|div|h[1-6])\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<\s*a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</\s*a>", r"\2 (\1)", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _letter_html(title: str, lead: str, btn_url: str, btn_text: str, note: str) -> str:
    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:24px;background:#0d1117;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;">
  <div style="max-width:520px;margin:0 auto;background:#161b22;border:1px solid #30363d;border-radius:16px;padding:32px;">
    <h2 style="margin:0 0 16px;color:#e6edf3;">🔐 {BRAND}</h2>
    <h3 style="margin:0 0 16px;color:#e6edf3;">{title}</h3>
    <p style="color:#8b949e;line-height:1.5;">{lead}</p>
    <p style="text-align:center;margin:28px 0;">
      <a href="{btn_url}" style="background:linear-gradient(135deg,#153a75,#0e5a61);color:#ffffff;text-decoration:none;padding:14px 32px;border-radius:999px;font-weight:bold;display:inline-block;">{btn_text}</a>
    </p>
    <p style="color:#8b949e;font-size:13px;line-height:1.5;">{note}</p>
    <p style="color:#6e7681;font-size:12px;word-break:break-all;">Если кнопка не работает, откройте ссылку вручную:<br>{btn_url}</p>
  </div>
</body></html>"""


_TOKEN_KIND_INFO = {
    "verify": (
        "Подтвердите почту — AmneziaWG VPN",
        "/auth/email/verify?token=",
        "Подтверждение почты",
        "Вы зарегистрировались на сайте AmneziaWG VPN. Нажмите кнопку, чтобы подтвердить этот адрес и активировать вход по почте.",
        "Подтвердить почту",
        "Ссылка действует 24 часа. Если это были не вы — просто проигнорируйте письмо.",
    ),
    "reset": (
        "Сброс пароля — AmneziaWG VPN",
        "/auth/email/reset?token=",
        "Сброс пароля",
        "Мы получили запрос на сброс пароля вашей учетной записи AmneziaWG VPN.",
        "Задать новый пароль",
        "Ссылка действует 1 час. Если запрос отправляли не вы — проигнорируйте письмо, пароль не изменится.",
    ),
    "link": (
        "Привязка почты — AmneziaWG VPN",
        "/auth/email/link?token=",
        "Привязка почты к аккаунту",
        "Кто-то (надеемся, вы) привязывает эту почту к аккаунту AmneziaWG VPN в Telegram-боте или на сайте.",
        "Привязать почту",
        "Ссылка действует 2 часа. После привязки вы сможете входить в личный кабинет сайта и по почте, и через Telegram.",
    ),
}


def send_token_mail(kind: str, to: str, token: str) -> tuple[bool, str]:
    """Письмо со ссылкой-токеном заданного вида (verify/reset/link)."""
    info = _TOKEN_KIND_INFO[kind]
    site_url = _s("site_url", "https://amneziawg.fun").rstrip("/")
    url = site_url + info[1] + token
    html = _letter_html(info[2], info[3], url, info[4], info[5])
    return send_email(to, info[0], html)


def send_email(to: str, subject: str, html: str) -> tuple[bool, str]:
    """Блокирующая отправка (вызывать через asyncio.to_thread). Возвращает (ok, error)."""
    mode = mail_mode()
    if mode == "off":
        raw_mode = db.get_setting("mail_mode")
        has_key = bool(db.get_setting("mail_resend_key"))
        has_host = bool(db.get_setting("mail_smtp_host"))
        return False, (
            f"Почта выключена настройкой. Диагностика базы: mail_mode={raw_mode!r} "
            f"(надо: auto), Resend-ключ: {'задан ✓' if has_key else 'НЕ ЗАДАН ✗'}, "
            f"SMTP-хост: {'задан' if has_host else 'не задан'}. "
            f"В таблице настроек найдите строку «Тип отправки почты (mail_mode)» и впишите: auto"
        )

    if mode == "resend":
        return _send_resend(to, subject, html)
    return _send_smtp(to, subject, html)


def _send_resend(to: str, subject: str, html: str) -> tuple[bool, str]:
    key = _s("mail_resend_key")
    sender = _s("mail_from")
    if not key or not sender:
        return False, "Resend: задайте mail_resend_key и mail_from"
    payload = {
        "from": sender,
        "to": [to],
        "subject": subject,
        "html": html,
        "text": html_to_text(html),
    }
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Cloudflare перед Resend режет дефолтный Python-urllib UA (403 error 1010),
            # поэтому выставляем обычный браузерный юзер-агент.
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return True, ""
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        if e.code == 403 and "1010" in body:
            return False, (
                "Cloudflare заблокировал запрос к Resend (код 1010) из-за репутации IP сервера. "
                "Вариант: используйте SMTP (Яндекс/Mail.ru) - задайте SMTP-строки и mail_mode=smtp."
            )
        return False, f"Resend HTTP {e.code}: {body}"
    except Exception as e:
        return False, f"Resend: {e}"


def _send_smtp(to: str, subject: str, html: str) -> tuple[bool, str]:
    host = _s("mail_smtp_host")
    port = int(_s("mail_smtp_port", "587") or 587)
    user = _s("mail_smtp_user")
    password = _s("mail_smtp_password")
    sender = _s("mail_from")
    tls = (_s("mail_smtp_tls", "starttls") or "starttls").lower()
    if not host or not sender:
        return False, "SMTP: задайте mail_smtp_host и mail_from"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["To"] = to
    msg.attach(MIMEText(html_to_text(html), "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    # "AmneziaWG VPN <no-reply@...>" -> части для formataddr
    if "<" in sender and ">" in sender:
        name = sender.split("<")[0].strip().strip('"')
        addr = sender.split("<")[1].rstrip(">").strip()
        msg["From"] = formataddr((str(Header(name, "utf-8")), addr))
        envelope_from = addr
    else:
        msg["From"] = sender
        envelope_from = sender

    try:
        if tls == "ssl" or port == 465:
            smtp = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            smtp = smtplib.SMTP(host, port, timeout=15)
        with smtp:
            if tls == "starttls":
                smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.sendmail(envelope_from, [to], msg.as_string())
        return True, ""
    except Exception as e:
        return False, f"SMTP {host}:{port}: {e}"
