# -*- coding: utf-8 -*-
"""
Тесты службы поддержки (единая переписка пользователь <-> администратор):

  - сообщения с сайта сохраняются в личном чате пользователя (каждый - свой чат);
  - администратор отвечает и через сайт (раздел «Чаты» веб-админки), и из Telegram
    (кнопка «Ответить») - история общая, ответы видны пользователю на сайте;
  - ответы для пользователей без Telegram (аккаунт по почте) не теряются: они
    сохраняются в чате, а не отправляются в никуда через Telegram API;
  - администратор может удалять чаты целиком;
  - при объединении почтового аккаунта с Telegram переписка переезжает вместе с ним.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
import base64
import json

import admin_panel
import database as db
import bot as bot_module
import support
import user_handlers
import web_admin
import web_app

pytestmark = pytest.mark.usefixtures("isolated_db")


class FakeBot:
    """Запоминает отправленные сообщения; может «падать» как недоступный Telegram."""

    def __init__(self):
        self.sent = []
        self.fail = False

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        self.sent.append((chat_id, text, kwargs))


def login(client, tg_id):
    client.cookies.clear()
    data = base64.b64encode(json.dumps({"tg_id": tg_id}).encode())
    client.cookies.set("vpn_session", TimestampSigner(web_app.SESSION_SECRET).sign(data).decode())


@pytest.fixture
def fake_bot():
    return FakeBot()


@pytest.fixture
def client(monkeypatch, fake_bot):
    monkeypatch.setattr(web_app, "bot", fake_bot)
    monkeypatch.setattr(bot_module, "bot", fake_bot)
    with TestClient(web_app.app, base_url="https://testserver") as client:
        yield client


@pytest.fixture
def admin_client(monkeypatch, fake_bot):
    # Отдельный TestClient (своя cookie-банка), чтобы сессии админа и пользователя
    # в одном тесте не затирали друг друга.
    monkeypatch.setattr(web_app, "bot", fake_bot)
    monkeypatch.setattr(bot_module, "bot", fake_bot)
    with TestClient(web_app.app, base_url="https://testserver") as client:
        login(client, web_admin.ROOT_ADMIN_ID)
        yield client


def accept_tos(client):
    r = client.post("/web/accept_tos", follow_redirects=False)
    assert r.status_code == 303


# ---------- СООБЩЕНИЯ С САЙТА ----------

def test_site_message_saved_in_user_chat_and_admin_notified(client, fake_bot):
    login(client, 111)
    r = client.post("/web/support", json={"text": "Привет, не работает конфиг <oops>"})
    assert r.status_code == 200
    assert r.json()["success"] is True
    assert r.json()["notified"] is True

    # сообщение сохранено в личном чате пользователя
    msgs = db.get_support_messages(111)
    assert len(msgs) == 1
    assert msgs[0][2] == "user"
    assert msgs[0][4] == "Привет, не работает конфиг <oops>"

    # админ получил уведомление с кнопками «Ответить» и «Чат на сайте»
    assert len(fake_bot.sent) == 1
    admin_chat, admin_text, admin_kw = fake_bot.sent[0]
    assert admin_chat == web_app.ADMIN_ID
    assert "Привет, не работает конфиг" in admin_text
    markup = admin_kw.get("reply_markup")
    buttons = [b for row in markup.inline_keyboard for b in row]
    callbacks = [b.callback_data for b in buttons if b.callback_data]
    urls = [b.url for b in buttons if b.url]
    assert "reply_to_111" in callbacks
    assert any("/admin/web/chats/111" in u for u in urls)


def test_site_message_saved_even_if_telegram_notification_fails(client, fake_bot):
    fake_bot.fail = True
    login(client, 222)
    r = client.post("/web/support", json={"text": "Сообщение при недоступном Telegram"})
    assert r.status_code == 200
    assert r.json() == {"success": True, "notified": False}
    # сообщение не потеряно
    assert len(db.get_support_messages(222)) == 1


def test_support_history_endpoint_requires_auth_and_returns_messages(client):
    login(client, 333)
    client.post("/web/support", json={"text": "Первое"})
    client.post("/web/support", json={"text": "Второе"})
    r = client.get("/web/support/messages")
    assert r.status_code == 200
    texts = [m["text"] for m in r.json()["messages"]]
    assert texts == ["Первое", "Второе"]
    assert all(m["sender"] == "user" for m in r.json()["messages"])

    client.cookies.clear()
    assert client.get("/web/support/messages").status_code == 401
    assert client.post("/web/support", json={"text": "x"}).status_code == 401


# ---------- ОТВЕТЫ АДМИНА ЧЕРЕЗ САЙТ ----------

def test_admin_reply_from_site_reaches_virtual_user_chat_not_telegram(admin_client, client, fake_bot):
    # пользователь, зарегистрированный на сайте по почте (без Telegram)
    acc_id = db.create_email_account("client@example.com", None)
    assert acc_id
    v_tg = db.ensure_email_account_tg("client@example.com")
    assert db.is_virtual_tg_id(v_tg)

    login(client, v_tg)
    client.post("/web/support", json={"text": "Вопрос от почтового клиента"})
    client.cookies.clear()

    # админ видит чат в списке
    page = admin_client.get("/admin/web/chats")
    assert page.status_code == 200
    assert "client@example.com" in page.text
    assert "Вопрос от почтового клиента" in page.text

    # и отвечает через сайт
    fake_bot.sent = []
    r = admin_client.post(f"/admin/web/chats/{v_tg}/reply", data={"text": "Ответ администратора"},
                          follow_redirects=False)
    assert r.status_code == 303

    # ответ сохранен в чате; в Telegram для виртуального ID ничего не слалось
    msgs = db.get_support_messages(v_tg)
    assert [m[2] for m in msgs] == ["user", "admin"]
    assert msgs[1][4] == "Ответ администратора"
    assert fake_bot.sent == []

    # пользователь видит ответ на сайте (история + кабинет)
    login(client, v_tg)
    history = client.get("/web/support/messages").json()["messages"]
    assert [m["text"] for m in history] == ["Вопрос от почтового клиента", "Ответ администратора"]
    accept_tos(client)
    dash = client.get("/dashboard")
    assert "Ответ администратора" in dash.text


def test_admin_reply_from_site_delivers_to_real_user_telegram(admin_client, client, fake_bot):
    login(client, 444)
    client.post("/web/support", json={"text": "Привет из кабинета"})
    client.cookies.clear()

    r = admin_client.post("/admin/web/chats/444/reply", data={"text": "Здравствуйте!"},
                          follow_redirects=False)
    assert r.status_code == 303
    assert [m[2] for m in db.get_support_messages(444)] == ["user", "admin"]
    # реальному пользователю продублировано в Telegram
    assert any(chat == 444 for chat, _t, _k in fake_bot.sent)
    assert any("Ответ поддержки" in t for _c, t, _k in fake_bot.sent)


def test_admin_chat_reply_and_delete_require_admin(client):
    # без входа - редирект на логин
    r = client.get("/admin/web/chats", follow_redirects=False)
    assert r.status_code in (303, 307)
    assert "/login" in r.headers.get("location", "")
    r = client.post("/admin/web/chats/555/reply", data={"text": "x"}, follow_redirects=False)
    assert r.status_code in (303, 307)

    # обычный пользователь без прав админа - 403
    login(client, 555)
    assert client.get("/admin/web/chats").status_code == 403
    assert client.post("/admin/web/chats/555/reply", data={"text": "x"}).status_code == 403
    assert client.post("/admin/web/chats/555/delete").status_code == 403


def test_admin_can_delete_chat(admin_client, client):
    login(client, 666)
    client.post("/web/support", json={"text": "Старое обращение"})
    client.cookies.clear()

    r = admin_client.post("/admin/web/chats/666/delete", follow_redirects=False)
    assert r.status_code == 303
    assert db.get_support_messages(666) == []
    page = admin_client.get("/admin/web/chats")
    assert "Старое обращение" not in page.text


# ---------- ОТВЕТЫ ИЗ TELEGRAM-БОТА ----------

@pytest.mark.asyncio
async def test_reply_and_deliver_saves_always_and_reports_delivery(fake_bot):
    # доставка успешна
    delivered, note = await support.reply_and_deliver(fake_bot, 777, "Здравствуйте!")
    assert delivered is True
    assert [m[2] for m in db.get_support_messages(777)] == ["admin"]
    assert fake_bot.sent and fake_bot.sent[0][0] == 777

    # Telegram недоступен - ответ все равно сохранен в чате
    fake_bot.fail = True
    delivered, note = await support.reply_and_deliver(fake_bot, 777, "Второй ответ")
    assert delivered is False
    assert note
    assert [m[4] for m in db.get_support_messages(777)] == ["Здравствуйте!", "Второй ответ"]


@pytest.mark.asyncio
async def test_reply_and_deliver_never_calls_telegram_for_virtual(fake_bot):
    db.create_email_account("no-tg@example.com", None)
    v_tg = db.ensure_email_account_tg("no-tg@example.com")
    delivered, note = await support.reply_and_deliver(fake_bot, v_tg, "Ответ")
    assert delivered is False
    assert "Telegram" in note or "телеграм" in note.lower()
    assert fake_bot.sent == []  # API не дёргался вообще
    assert [m[4] for m in db.get_support_messages(v_tg)] == ["Ответ"]


@pytest.mark.asyncio
async def test_telegram_support_message_saved_into_chat(fake_bot, monkeypatch):
    """Сообщение в поддержку из бота попадает в общий чат и уведомляет админа."""
    tg_id = 888
    message = SimpleNamespace(
        text="Помогите из бота",
        from_user=SimpleNamespace(id=tg_id, full_name="Тестовый Юзер"),
        bot=fake_bot,
        answer=AsyncMock(),
    )
    state = SimpleNamespace(update_data=AsyncMock(), set_state=AsyncMock(), clear=AsyncMock(),
                            get_data=AsyncMock(return_value={}))
    await user_handlers.process_support_message(message, state)

    assert [m[4] for m in db.get_support_messages(tg_id)] == ["Помогите из бота"]
    # уведомление админу ушло с кнопкой «Ответить»
    assert fake_bot.sent and fake_bot.sent[0][0] == web_app.ADMIN_ID
    markup = fake_bot.sent[0][2].get("reply_markup")
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert any(b.callback_data == "reply_to_888" for b in buttons)


@pytest.mark.asyncio
async def test_non_text_message_in_support_does_not_break(fake_bot):
    """Фото/файл вместо текста не роняет бота и не пишет пустых сообщений в чат."""
    message = SimpleNamespace(
        text=None, caption=None,
        from_user=SimpleNamespace(id=901, full_name="Тестовый Юзер"),
        bot=fake_bot,
        answer=AsyncMock(),
    )
    state = SimpleNamespace(update_data=AsyncMock(), set_state=AsyncMock(), clear=AsyncMock(),
                            get_data=AsyncMock(return_value={}))
    await user_handlers.process_support_message(message, state)
    assert db.get_support_messages(901) == []
    assert fake_bot.sent == []
    state.clear.assert_not_called()  # пользователю дали шанс написать текстом


@pytest.mark.asyncio
async def test_telegram_admin_send_reply_saves_and_reports(fake_bot, monkeypatch):
    """Ответ из Telegram-админки: сохраняется в чате; при недоступной доставке
    админу приходит понятный статус, а не «Ошибка доставки»."""
    message = SimpleNamespace(
        text="Ответ из телеграм-админки",
        bot=fake_bot,
        answer=AsyncMock(),
    )
    state = SimpleNamespace(
        update_data=AsyncMock(),
        set_state=AsyncMock(),
        clear=AsyncMock(),
        get_data=AsyncMock(return_value={"reply_user_id": "999"}),
    )
    await admin_panel.send_reply(message, state)
    assert [m[4] for m in db.get_support_messages(999)] == ["Ответ из телеграм-админки"]
    assert fake_bot.sent and fake_bot.sent[0][0] == 999

    # теперь Telegram недоступен: ответ сохранен, админа информируют о чате на сайте
    fake_bot.sent = []
    fake_bot.fail = True
    state.get_data = AsyncMock(return_value={"reply_user_id": "999"})
    await admin_panel.send_reply(message, state)
    assert [m[4] for m in db.get_support_messages(999)] == ["Ответ из телеграм-админки"] * 2
    answers = [c.args[0] for c in message.answer.await_args_list]
    assert any("сохранён в чате на сайте" in a for a in answers)


# ---------- ПРОЧЕЕ ----------

def test_merge_moves_support_history_to_real_account():
    db.create_email_account("merge@example.com", None)
    v_tg = db.ensure_email_account_tg("merge@example.com")
    db.add_support_message(v_tg, "user", "Вопрос до привязки", sender_name="merge@example.com")
    db.add_support_message(v_tg, "admin", "Ответ до привязки")

    assert db.merge_virtual_into_real(v_tg, 123456789) is True
    msgs = db.get_support_messages(123456789)
    assert [m[4] for m in msgs] == ["Вопрос до привязки", "Ответ до привязки"]
    assert db.get_support_messages(v_tg) == []


def test_main_menu_has_site_button():
    """Кнопка «Бесплатный доступ» переименована в «Сайт» и ведет на сайт."""
    db.update_setting("site_url", "https://example.org/")
    markup = user_handlers.get_main_keyboard()
    buttons = [b for row in markup.inline_keyboard for b in row]
    texts = [b.text for b in buttons]
    assert "🌐 Сайт" in texts
    assert not any("Бесплатный доступ" in t for t in texts)
    site_btn = next(b for b in buttons if b.text == "🌐 Сайт")
    assert site_btn.url == "https://example.org"


def test_admin_chat_page_shows_thread(admin_client, client):
    login(client, 1111)
    client.post("/web/support", json={"text": "Мой вопрос"})
    client.cookies.clear()
    admin_client.post("/admin/web/chats/1111/reply", data={"text": "Мой ответ"})
    page = admin_client.get("/admin/web/chats/1111")
    assert page.status_code == 200
    assert "Мой вопрос" in page.text
    assert "Мой ответ" in page.text
    assert "Удалить чат" in page.text
