"""
Служба поддержки: единая переписка пользователя и администратора.

У каждого пользователя свой чат (все сообщения с одним tg_id). Сообщения хранятся
в таблице support_messages и видны:
  - пользователю - в личном кабинете на сайте (раздел «Поддержка»);
  - администратору - в разделе «Чаты» веб-админки и по кнопке «Ответить» под
    уведомлением в Telegram.

Отвечать можно и из Telegram, и через сайт - история общая. Доставка ответа в
Telegram всегда дополнительная (best effort): пользователи, зарегистрированные
на сайте по почте, не имеют Telegram-аккаунта (виртуальный ID), а реальный
пользователь мог просто не открывать бота. Ответ в любом случае сохраняется в
чате и виден на сайте - поэтому переписка не теряется.
"""

import database as db
from aiogram import html as tg_html
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

USER = "user"
ADMIN = "admin"


def user_display_name(tg_id):
    """Отображаемое имя пользователя: имя из профиля, иначе почта, иначе ID."""
    name = ""
    try:
        profile = db.get_user_profile(tg_id)
        if profile and len(profile) > 1 and profile[1]:
            name = str(profile[1]).strip()
    except Exception:
        pass
    email = None
    try:
        acc = (db.get_email_account_by_virtual_tg(tg_id) if db.is_virtual_tg_id(tg_id)
               else db.get_email_account_by_tg(tg_id))
        email = acc[1] if acc else None
    except Exception:
        email = None
    if name and email and name != email:
        return f"{name} ({email})"
    return name or email or str(tg_id)


def save_user_message(tg_id, text, sender_name=None):
    """Сообщение пользователя (с сайта или из бота) -> id записи в чате."""
    return db.add_support_message(
        tg_id, USER, text,
        sender_name=sender_name or user_display_name(tg_id))


def save_admin_reply(tg_id, text, sender_name="Администратор"):
    """Ответ администратора (с сайта или из бота) -> id записи в чате."""
    return db.add_support_message(tg_id, ADMIN, text, sender_name=sender_name)


async def deliver_to_user(bot, tg_id, text):
    """Дублирует ответ пользователю в Telegram (best effort).

    Возвращает (доставлено: bool, пояснение: str). Для пользователей без
    Telegram (виртуальный ID) доставка не пытается выполниться.
    """
    if db.is_virtual_tg_id(tg_id):
        return False, "у пользователя нет Telegram (аккаунт на сайте по почте)"
    try:
        await bot.send_message(
            tg_id,
            f"👨‍💻 <b>Ответ поддержки:</b>\n\n{tg_html.quote(text)}",
            parse_mode="HTML")
        return True, ""
    except Exception as e:
        return False, f"{e}"


async def reply_and_deliver(bot, tg_id, text, sender_name="Администратор"):
    """Ответ администратора: сохранить в чате + попытаться доставить в Telegram.

    Возвращает (delivered: bool, note: str). Ответ сохраняется ВСЕГДА, даже если
    Telegram недоступен - пользователь увидит его на сайте.
    """
    save_admin_reply(tg_id, text, sender_name=sender_name)
    delivered, note = await deliver_to_user(bot, tg_id, text)
    return delivered, note


def admin_reply_keyboard(user_tg_id, site_url=None):
    """Кнопки под уведомлением админу: ответить + открыть чат на сайте."""
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(
        text="✍️ Ответить", callback_data=f"reply_to_{int(user_tg_id)}"))
    if site_url:
        builder.add(InlineKeyboardButton(
            text="💬 Чат на сайте", url=f"{site_url.rstrip('/')}/admin/web/chats/{int(user_tg_id)}"))
    builder.adjust(1)
    return builder.as_markup()


def user_identity_note(tg_id):
    """Строка для уведомлений админу: почта (если известна) и пометка для
    аккаунтов без Telegram (ответ через бота до них не дойдет - только сайт)."""
    email = None
    try:
        if db.is_virtual_tg_id(tg_id):
            acc = db.get_email_account_by_virtual_tg(tg_id)
        else:
            acc = db.get_email_account_by_tg(tg_id)
        email = acc[1] if acc else None
    except Exception:
        email = None
    lines = []
    if email:
        lines.append(f"📧 Почта: <b>{tg_html.quote(email)}</b>")
    if db.is_virtual_tg_id(tg_id):
        lines.append("⚠️ Клиент без Telegram (вход по почте): ответ через бота не дойдет - "
                     "он увидит ответ в чате на сайте.")
    return "\n".join(lines)
