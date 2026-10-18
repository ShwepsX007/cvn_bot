# -*- coding: utf-8 -*-
"""
Тесты учета и лимитов трафика:

  - разбор счётчиков awg (конфиг с '# Peer: user_xxx' + 'awg show dump');
  - накопление расхода по дельтам (переживает сброс счётчиков) и сброс при оплате;
  - синхронизация с нод по SSH;
  - контроль лимитов: предупреждение при подходе к лимиту (однократно) и отключение
    с уведомлениями (кабинет + Telegram) при исчерпании - как после истечения срока;
  - управление в веб-админке: общий лимит, личные лимиты, «ко всем сразу», сброс;
  - лимиты трафика в описании сервиса.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
import base64
import json

import admin_panel
import bot as bot_module
import database as db
import ssh_manager
import traffic
import user_handlers
import web_admin
import web_app

pytestmark = pytest.mark.usefixtures("isolated_db")

GB = 1024 ** 3

FAKE_CONF = """
[Interface]
PrivateKey = SERVERPRIV
Address = 10.8.0.1/24

# Peer: user_111
[Peer]
PublicKey = PUBLIKELEVEN
PresharedKey = PSK1
AllowedIPs = 10.8.0.3/32

# Peer: user_222
[Peer]
PublicKey = PUBLIKETWOTWO
PresharedKey = PSK2
AllowedIPs = 10.8.0.4/32

# Peer: free7
[Peer]
PublicKey = PUBLIKFREE
AllowedIPs = 10.8.0.5/32
"""

FAKE_DUMP = """SERVERPRIV\tSERVERPUB\t22222\toff
PUBLIKELEVEN\tPSK1\t1.2.3.4:1000\t10.8.0.3/32\t1700000000\t1048576\t2097152\t25
PUBLIKETWOTWO\tPSK2\t5.6.7.8:2000\t10.8.0.4/32\t1700000000\t5000000000\t1000\t25
PUBLIKFREE\t\t9.9.9.9:3000\t10.8.0.5/32\t1700000000\t123\t456\t25
"""

# Полный ответ команды traffic.TRAFFIC_DUMP_CMD с ноды
FAKE_OUT = f"---CONF---\n{FAKE_CONF}\n---DUMP---\n{FAKE_DUMP}"

FAKE_CFG = "[Interface]\nAddress = 10.8.0.99/32\nPrivateKey = CLIENT\n\n[Peer]\nPublicKey = SPUB\nAllowedIPs = 0.0.0.0/0\n"


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(("msg", chat_id, text, kwargs))

    async def send_document(self, chat_id, file, **kwargs):
        self.sent.append(("doc", chat_id, kwargs))

    async def send_photo(self, chat_id, photo, **kwargs):
        self.sent.append(("photo", chat_id, kwargs))


@pytest.fixture
def fake_bot():
    return FakeBot()


@pytest.fixture
def client(monkeypatch, fake_bot):
    monkeypatch.setattr(web_app, "bot", fake_bot)
    monkeypatch.setattr(bot_module, "bot", fake_bot)
    with TestClient(web_app.app, base_url="https://testserver") as client:
        yield client


def login(client, tg_id):
    client.cookies.clear()
    data = base64.b64encode(json.dumps({"tg_id": tg_id}).encode())
    client.cookies.set("vpn_session", TimestampSigner(web_app.SESSION_SECRET).sign(data).decode())


@pytest.fixture
def admin_client(monkeypatch, fake_bot):
    monkeypatch.setattr(web_app, "bot", fake_bot)
    monkeypatch.setattr(bot_module, "bot", fake_bot)
    with TestClient(web_app.app, base_url="https://testserver") as client:
        login(client, web_admin.ROOT_ADMIN_ID)
        yield client


def add_server(name="🌍 Сервер", ip="10.9.9.9"):
    db.add_server(ip, name, 2222)
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM servers WHERE ip=?", (ip,))
    sid = cur.fetchone()[0]
    conn.close()
    return sid


def fake_ssh_factory(responses=None):
    """responses: строка-ответ, список ответов по порядку или функция от cmd."""
    calls = []

    async def fake_ssh(ip, port, cmd, **kw):
        calls.append((ip, port, cmd))
        if callable(responses):
            return responses(cmd)
        if isinstance(responses, str):
            return responses
        if responses is None:
            return FAKE_OUT
        return responses.pop(0) if responses else FAKE_OUT

    fake_ssh.calls = calls
    return fake_ssh


# ---------- РАЗБОР СЧЕТЧИКОВ ----------

def test_parse_peer_traffic_maps_usernames():
    peers = traffic.parse_peer_traffic(FAKE_CONF, FAKE_DUMP)
    assert peers["user_111"] == (1048576, 2097152)
    assert peers["user_222"] == (5000000000, 1000)
    assert peers["free7"] == (123, 456)  # free-пир тоже виден, но в sync отфильтруется
    assert traffic.username_to_tg_id("user_111") == 111
    assert traffic.username_to_tg_id("free7") is None


def test_resolve_limit_bytes_semantics():
    db.update_setting("traffic_limit_gb", "5")
    assert traffic.resolve_limit_bytes(None) == 5 * GB        # общий по умолчанию
    assert traffic.resolve_limit_bytes(0) == 0                # без лимита
    assert traffic.resolve_limit_bytes(10) == 10 * GB         # личный
    assert traffic.resolve_limit_bytes(2.5) == int(2.5 * GB)  # дробный
    db.update_setting("traffic_limit_gb", "0")
    assert traffic.resolve_limit_bytes(None) == 0
    assert traffic.resolve_limit_bytes("мусор") == 0


def test_format_bytes():
    assert traffic.format_bytes(0) == "0 Б"
    assert traffic.format_bytes(5 * GB) == "5.00 ГБ"
    assert traffic.format_bytes(1500) == "1.46 КБ"


# ---------- НАКОПЛЕНИЕ РАСХОДА ----------

def test_apply_counters_accumulates_delta_and_handles_reset():
    sid = add_server()
    db.add_or_update_user(111, sid, "user_111", days=30)

    used, delta = db.apply_user_traffic_counters(111, sid, 1000, 500)
    assert (used, delta) == (1500, 1500)      # первый замер
    used, delta = db.apply_user_traffic_counters(111, sid, 1300, 700)
    assert (used, delta) == (2000, 500)       # дельта 300 + 200

    # сброс счётчиков на сервере (пересоздание пира): весь текущий объем - новый
    used, delta = db.apply_user_traffic_counters(111, sid, 10, 5)
    assert (used, delta) == (2015, 15)

    used, delta = db.apply_user_traffic_counters(111, sid, 20, 5)
    assert (used, delta) == (2025, 10)


def test_reset_user_traffic_on_payment():
    sid = add_server()
    db.add_or_update_user(111, sid, "user_111", days=30)
    db.apply_user_traffic_counters(111, sid, 5 * GB, 5 * GB)
    db.mark_traffic_warned(111, sid)
    db.mark_traffic_over(111, sid)

    # продление: расход/флаги обнуляются, счётчики сервера сохраняются
    db.reset_user_traffic(111, sid)
    tr = db.get_user_traffic(111, sid)
    assert tr[1] == 0 and tr[4] == 0 and tr[5] == 0
    assert tr[2] == 5 * GB and tr[3] == 5 * GB

    # новая выдача (пир пересоздан): обнуляется всё
    db.reset_user_traffic(111, sid, full=True)
    tr = db.get_user_traffic(111, sid)
    assert tr[1] == 0 and tr[2] == 0 and tr[3] == 0


@pytest.mark.asyncio
async def test_sync_all_updates_known_users_only():
    sid = add_server()
    db.add_or_update_user(111, sid, "user_111", days=30)
    db.add_or_update_user(222, sid, "user_222", days=30)

    fake_ssh = fake_ssh_factory()
    updated = await traffic.sync_all(ssh_fn=fake_ssh)
    assert updated == 2  # free7 пропущен
    assert db.get_user_traffic(111, sid)[1] == 1048576 + 2097152
    assert db.get_user_traffic(222, sid)[1] == 5000000000 + 1000
    assert fake_ssh.calls  # ssh вызывался
    assert "awg show awg0 dump" in fake_ssh.calls[0][2]


# ---------- КОНТРОЛЬ ЛИМИТОВ (ФОНОВАЯ ПРОВЕРКА) ----------

@pytest.mark.asyncio
async def test_over_limit_disables_notifies_and_removes_peer(monkeypatch, fake_bot):
    sid = add_server("🇩🇪 Германия")
    db.add_or_update_user(111, sid, "user_111", days=30)
    db.set_user_traffic_limit(111, sid, 1)  # 1 ГБ
    db.update_setting("traffic_warn_percent", "0")  # без предупреждений для чистоты

    # пользователь израсходовал 2 ГБ (счётчики в dump)
    dump = FAKE_OUT.replace("1048576\t2097152", f"{2 * GB}\t0")
    fake_ssh = fake_ssh_factory(lambda cmd: "ok" if "remove_user.sh" in cmd else dump)
    monkeypatch.setattr(bot_module, "bot", fake_bot)
    monkeypatch.setattr(ssh_manager, "run_ssh_command", fake_ssh)
    monkeypatch.setattr(bot_module.ssh, "run_ssh_command", fake_ssh)

    await bot_module.check_traffic_limits()

    # доступ отключен, флаг исчерпания поставлен
    sub = db.get_user_sub(111, sid)
    assert sub[5] == 0
    assert db.get_user_traffic(111, sid)[5] == 1
    # пир снят с сервера
    assert any("remove_user.sh" in c for _i, _p, c in fake_ssh.calls)
    # уведомления: пользователю и админу
    texts = [t for kind, chat, t, _k in fake_bot.sent if kind == "msg"]
    assert any("Лимит трафика исчерпан" in t and chat == 111 for kind, chat, t, _k in fake_bot.sent)
    assert any(chat == web_app.ADMIN_ID for kind, chat, _t, _k in fake_bot.sent)

    # повторный прогон ничего не дублирует (подписка уже неактивна)
    fake_bot.sent = []
    await bot_module.check_traffic_limits()
    assert fake_bot.sent == []


@pytest.mark.asyncio
async def test_warns_once_at_threshold(monkeypatch, fake_bot):
    sid = add_server()
    db.add_or_update_user(111, sid, "user_111", days=30)
    db.set_user_traffic_limit(111, sid, 10)  # 10 ГБ
    db.update_setting("traffic_warn_percent", "80")

    # 8.5 ГБ из 10 - порог 80% превышен, лимит еще нет
    dump = FAKE_OUT.replace("1048576\t2097152", f"{int(8.5 * GB)}\t0")
    fake_ssh = fake_ssh_factory(dump)
    monkeypatch.setattr(bot_module, "bot", fake_bot)
    monkeypatch.setattr(ssh_manager, "run_ssh_command", fake_ssh)
    monkeypatch.setattr(bot_module.ssh, "run_ssh_command", fake_ssh)

    await bot_module.check_traffic_limits()
    warns = [t for kind, chat, t, _k in fake_bot.sent if "почти исчерпан" in t]
    assert len(warns) == 1
    assert db.get_user_traffic(111, sid)[4] == 1  # warned
    assert db.get_user_sub(111, sid)[5] == 1       # еще активна

    # повторно предупреждение не шлется
    fake_bot.sent = []
    await bot_module.check_traffic_limits()
    assert fake_bot.sent == []

    # а при исчерпании - отключает
    dump2 = FAKE_OUT.replace("1048576\t2097152", f"{11 * GB}\t0")
    fake_ssh2 = fake_ssh_factory(lambda cmd: "ok" if "remove_user.sh" in cmd else dump2)
    monkeypatch.setattr(bot_module, "bot", fake_bot)
    monkeypatch.setattr(ssh_manager, "run_ssh_command", fake_ssh2)
    monkeypatch.setattr(bot_module.ssh, "run_ssh_command", fake_ssh2)
    await bot_module.check_traffic_limits()
    assert db.get_user_sub(111, sid)[5] == 0
    assert db.get_user_traffic(111, sid)[5] == 1


@pytest.mark.asyncio
async def test_unlimited_users_never_disabled(monkeypatch, fake_bot):
    sid = add_server()
    db.add_or_update_user(111, sid, "user_111", days=30)
    db.update_setting("traffic_limit_gb", "0")  # без лимитов
    dump = FAKE_OUT.replace("1048576\t2097152", f"{100 * GB}\t0")
    fake_ssh = fake_ssh_factory(dump)
    monkeypatch.setattr(bot_module, "bot", fake_bot)
    monkeypatch.setattr(ssh_manager, "run_ssh_command", fake_ssh)
    monkeypatch.setattr(bot_module.ssh, "run_ssh_command", fake_ssh)

    await bot_module.check_traffic_limits()
    assert db.get_user_sub(111, sid)[5] == 1
    assert fake_bot.sent == []


# ---------- СБРОС ЛИМИТА ПРИ ОПЛАТЕ ----------

@pytest.mark.asyncio
async def test_issue_vpn_access_resets_traffic_on_renewal(monkeypatch, fake_bot):
    sid = add_server()
    db.add_or_update_user(111, sid, "user_111", days=30)  # активная подписка -> продление
    db.apply_user_traffic_counters(111, sid, 3 * GB, 0)
    db.mark_traffic_warned(111, sid)

    ok, msg = await user_handlers.issue_vpn_access(fake_bot, 111, sid, "30d")
    assert ok
    tr = db.get_user_traffic(111, sid)
    assert tr[1] == 0 and tr[4] == 0          # расход обнулен
    assert tr[2] == 3 * GB                     # счётчики сервера сохранены (пир жив)
    assert any(kind == "msg" and chat == 111 for kind, chat, *_ in fake_bot.sent)


@pytest.mark.asyncio
async def test_issue_vpn_access_resets_traffic_on_new_issue(monkeypatch, fake_bot):
    sid = add_server()
    fake_ssh = fake_ssh_factory(lambda cmd: FAKE_CFG if "add_user.sh" in cmd else "ok")
    monkeypatch.setattr(ssh_manager, "run_ssh_command", fake_ssh)
    monkeypatch.setattr(user_handlers.ssh, "run_ssh_command", fake_ssh)

    # подписки нет -> новая выдача с генерацией конфига
    ok, msg = await user_handlers.issue_vpn_access(fake_bot, 111, sid, "1d")
    assert ok
    tr = db.get_user_traffic(111, sid)
    assert tr is not None
    assert tr[1] == 0 and tr[2] == 0 and tr[3] == 0  # всё с нуля
    assert any(kind == "doc" and chat == 111 for kind, chat, *_ in fake_bot.sent)


# ---------- УПРАВЛЕНИЕ В АДМИНКЕ ----------

def test_admin_traffic_page_shows_usage_and_limits(admin_client):
    sid = add_server("🇫🇮 Финляндия")
    db.add_or_update_user(111, sid, "user_111", days=30)
    db.apply_user_traffic_counters(111, sid, 2 * GB, 0)
    db.set_user_traffic_limit(111, sid, 5)

    page = admin_client.get("/admin/web/traffic")
    assert page.status_code == 200
    assert "2.00 ГБ" in page.text
    assert "5 ГБ" in page.text
    assert "Финляндия" in page.text


def test_admin_traffic_actions(admin_client):
    sid = add_server()
    db.add_or_update_user(111, sid, "user_111", days=30)
    db.add_or_update_user(222, sid, "user_222", days=30)

    # личный лимит
    r = admin_client.post("/admin/web/traffic/limit",
                          data={"tg_id": 111, "server_id": sid, "limit_gb": "7,5"},
                          follow_redirects=False)
    assert r.status_code == 303
    assert db.get_user_traffic(111, sid)[0] == 7.5

    # пустое поле = общий по умолчанию
    admin_client.post("/admin/web/traffic/limit",
                      data={"tg_id": 111, "server_id": sid, "limit_gb": ""})
    assert db.get_user_traffic(111, sid)[0] is None

    # ко всем сразу
    admin_client.post("/admin/web/traffic/apply-all", data={"limit_gb": "50"})
    assert db.get_user_traffic(111, sid)[0] == 50
    assert db.get_user_traffic(222, sid)[0] == 50

    # сброс личных лимитов к общему
    admin_client.post("/admin/web/traffic/clear-all")
    assert db.get_user_traffic(111, sid)[0] is None

    # общий лимит и процент предупреждения
    admin_client.post("/admin/web/traffic/settings",
                      data={"global_limit_gb": "30", "warn_percent": "90"})
    assert db.get_setting("traffic_limit_gb") == "30"
    assert db.get_setting("traffic_warn_percent") == "90"

    # сброс счётчика
    db.apply_user_traffic_counters(111, sid, 3 * GB, 0)
    admin_client.post("/admin/web/traffic/reset", data={"tg_id": 111, "server_id": sid})
    assert db.get_user_traffic(111, sid)[1] == 0

    # невалидный лимит не проходит
    r = admin_client.post("/admin/web/traffic/limit",
                          data={"tg_id": 111, "server_id": sid, "limit_gb": "abc"},
                          follow_redirects=True)
    assert "Лимит должен быть числом" in r.text


def test_admin_traffic_requires_admin(client):
    login(client, 555)
    assert client.get("/admin/web/traffic").status_code == 403
    assert client.post("/admin/web/traffic/apply-all", data={"limit_gb": "1"}).status_code == 403


@pytest.mark.asyncio
async def test_telegram_admin_traffic_panel(fake_bot):
    """Обзорная панель трафика в Telegram-админке (полное управление - на сайте)."""
    sid = add_server("🇩🇪 Германия")
    db.add_or_update_user(111, sid, "user_111", days=30)
    db.apply_user_traffic_counters(111, sid, 2 * GB, 0)
    db.set_user_traffic_limit(111, sid, 5)
    db.update_setting("traffic_limit_gb", "30")

    message = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    callback = SimpleNamespace(message=message, answer=AsyncMock(), bot=fake_bot)
    await admin_panel.show_traffic(callback)

    text = message.edit_text.await_args.args[0]
    assert "ТРАФИК ПОЛЬЗОВАТЕЛЕЙ" in text
    assert "30 ГБ" in text                      # общий лимит
    assert "<code>111</code>" in text
    assert "2.00 ГБ из 5.00 ГБ" in text         # расход / лимит
    assert "admin/web/traffic" in str(message.edit_text.await_args)


# ---------- ВОССТАНОВЛЕНИЕ ПОСЛЕ ОШИБОЧНОГО ЛИМИТА ----------

FAKE_CLIENT_CFG = (
    "[Interface]\n"
    "Address = 10.8.0.7/32\n"
    "DNS = 1.1.1.1\n"
    "PrivateKey = CLIENTPRIVKEY123\n\n"
    "[Peer]\n"
    "PublicKey = SERVERPUB\n"
    "PresharedKey = PSKVALUE456\n"
    "Endpoint = 1.2.3.4:22222\n"
    "AllowedIPs = 0.0.0.0/0\n"
)


def cut_for_traffic(tg_id, sid, used_bytes, limit_gb):
    """Симулирует результат срабатывания лимита: расход, отключение, флаг over."""
    db.add_or_update_user(tg_id, sid, f"user_{tg_id}", days=30)
    db.apply_user_traffic_counters(tg_id, sid, used_bytes, 0)
    if limit_gb is not None:
        db.set_user_traffic_limit(tg_id, sid, limit_gb)
    db.deactivate_user(tg_id, sid)
    db.mark_traffic_over(tg_id, sid)


def test_parse_client_config_and_restore_cmd():
    priv, psk, ip = traffic.parse_client_config(FAKE_CLIENT_CFG)
    assert (priv, psk, ip) == ("CLIENTPRIVKEY123", "PSKVALUE456", "10.8.0.7")
    cmd = traffic.build_restore_peer_cmd("user_111", priv, psk, ip)
    assert "awg pubkey" in cmd                 # ключ вычисляется на ноде из приватного
    assert "user_111" in cmd                    # пир подписывается именем для remove/traffic
    assert "10.8.0.7/32" in cmd
    assert "PSKVALUE456" in cmd
    assert "OK restored" in cmd


def test_admin_raise_limit_restores_user_same_keys(admin_client, fake_bot):
    """Сценарий ошибки: лимит 3 ГБ вместо 3000, доступ отключился; админ поднял
    лимит - доступ возвращается без оплаты, конфиг клиента продолжает работать."""
    sid = add_server("🇳🇱 Нидерланды")
    cut_for_traffic(111, sid, used_bytes=4 * GB, limit_gb=3)
    db.set_user_config(111, sid, FAKE_CLIENT_CFG)
    assert db.get_user_sub(111, sid)[5] == 0  # отрезан

    fake_ssh = fake_ssh_factory(lambda cmd: "OK restored user_111 PUB" if "awg pubkey" in cmd
                                else ("ok" if "remove_user.sh" in cmd else FAKE_OUT))
    import unittest.mock as _mock
    with _mock.patch("ssh_manager.run_ssh_command", fake_ssh):
        r = admin_client.post("/admin/web/traffic/limit",
                              data={"tg_id": 111, "server_id": sid, "limit_gb": "3000"},
                              follow_redirects=True)

    # доступ вернулся, флаги сняты, срок не тронут
    sub = db.get_user_sub(111, sid)
    assert sub[5] == 1
    assert db.get_user_traffic(111, sid)[5] == 0
    assert "восстановлен автоматически" in r.text or "Доступ восстановлен" in r.text

    # пир возвращен ПРЕЖНИМИ ключами (перевыдача add_user.sh НЕ понадобилась)
    restore_calls = [c for _i, _p, c in fake_ssh.calls if "awg pubkey" in c]
    assert restore_calls
    assert "CLIENTPRIVKEY123" in restore_calls[0]   # ключ для awg pubkey на ноде
    assert not any("add_user.sh" in c for _i, _p, c in fake_ssh.calls)

    # уведомления: пользователю и админу
    assert any(kind == "msg" and chat == 111 and "Доступ восстановлен" in t
               for kind, chat, t, _k in fake_bot.sent)
    assert any(kind == "msg" and chat == web_app.ADMIN_ID for kind, chat, t, _k in fake_bot.sent)


def test_restore_falls_back_to_reissue_without_saved_config(admin_client, fake_bot):
    """Без сохраненного конфига доступ восстанавливается перевыдачей - новый файл
    сохраняется в кабинете и отправляется пользователю."""
    sid = add_server()
    cut_for_traffic(222, sid, used_bytes=5 * GB, limit_gb=3)

    fake_ssh = fake_ssh_factory(lambda cmd: FAKE_CFG if "add_user.sh" in cmd else "ok")
    import unittest.mock as _mock
    with _mock.patch("ssh_manager.run_ssh_command", fake_ssh):
        admin_client.post("/admin/web/traffic/limit",
                          data={"tg_id": 222, "server_id": sid, "limit_gb": "3000"})

    assert db.get_user_sub(222, sid)[5] == 1
    assert db.get_user_config(222, sid) == FAKE_CFG  # новый конфиг сохранен для скачивания
    # пользователю отправлен новый конфиг в Telegram
    assert any(kind == "doc" and chat == 222 for kind, chat, *_ in fake_bot.sent)


def test_no_restore_while_still_over_limit(admin_client, fake_bot):
    sid = add_server()
    cut_for_traffic(111, sid, used_bytes=10 * GB, limit_gb=3)
    db.set_user_config(111, sid, FAKE_CLIENT_CFG)

    import unittest.mock as _mock
    with _mock.patch("ssh_manager.run_ssh_command", fake_ssh_factory()):
        admin_client.post("/admin/web/traffic/limit",
                          data={"tg_id": 111, "server_id": sid, "limit_gb": "8"})  # 8 < 10 ГБ
    assert db.get_user_sub(111, sid)[5] == 0  # всё ещё отключен
    assert db.get_user_traffic(111, sid)[5] == 1


def test_no_restore_when_subscription_time_expired(admin_client):
    sid = add_server()
    cut_for_traffic(111, sid, used_bytes=4 * GB, limit_gb=3)
    db.set_user_config(111, sid, FAKE_CLIENT_CFG)
    # срок истек (например, пока разбирались с лимитом)
    conn = db.get_conn()
    conn.execute("UPDATE users SET expire_date='2020-01-01 00:00:00' WHERE tg_id=? AND server_id=?", (111, sid))
    conn.commit()
    conn.close()

    import unittest.mock as _mock
    with _mock.patch("ssh_manager.run_ssh_command", fake_ssh_factory()):
        admin_client.post("/admin/web/traffic/limit",
                          data={"tg_id": 111, "server_id": sid, "limit_gb": "3000"})
    assert db.get_user_sub(111, sid)[5] == 0  # не восстановлен: срок честно истек


@pytest.mark.asyncio
async def test_periodic_job_restores_after_global_limit_fix(monkeypatch, fake_bot):
    """Порог сработал по общему лимиту (настройка «по умолчанию») - после починки
    общей настройки фоновая проверка тоже возвращает пользователей в строй."""
    sid = add_server()
    db.update_setting("traffic_limit_gb", "3")
    db.update_setting("traffic_warn_percent", "0")
    db.add_or_update_user(111, sid, "user_111", days=30)
    db.apply_user_traffic_counters(111, sid, 4 * GB, 0)
    db.deactivate_user(111, sid)
    db.mark_traffic_over(111, sid)

    # админ починил общий лимит прямо в настройках (не через страницу «Трафик»)
    db.update_setting("traffic_limit_gb", "3000")

    fake_ssh = fake_ssh_factory(lambda cmd: "OK restored" if "awg pubkey" in cmd else FAKE_OUT)
    monkeypatch.setattr(bot_module, "bot", fake_bot)
    monkeypatch.setattr(ssh_manager, "run_ssh_command", fake_ssh)
    monkeypatch.setattr(bot_module.ssh, "run_ssh_command", fake_ssh)

    await bot_module.check_traffic_limits()
    assert db.get_user_sub(111, sid)[5] == 1
    assert db.get_user_traffic(111, sid)[5] == 0
    assert any("Доступ восстановлен" in t for kind, chat, t, _k in fake_bot.sent if kind == "msg")


@pytest.mark.asyncio
async def test_counter_reset_restores_and_warn_flag_cleared_on_raise(monkeypatch, fake_bot):
    sid = add_server()
    cut_for_traffic(111, sid, used_bytes=4 * GB, limit_gb=3)
    db.set_user_config(111, sid, FAKE_CLIENT_CFG)
    # второй пользователь: только предупрежден, лимит подняли - флаг снимается
    db.add_or_update_user(222, sid, "user_222", days=30)
    db.set_user_traffic_limit(222, sid, 3)
    db.apply_user_traffic_counters(222, sid, int(2.5 * GB), 0)
    db.mark_traffic_warned(222, sid)

    fake_ssh = fake_ssh_factory(lambda cmd: "OK restored" if "awg pubkey" in cmd else "ok")
    monkeypatch.setattr(bot_module, "bot", fake_bot)
    monkeypatch.setattr(ssh_manager, "run_ssh_command", fake_ssh)

    # сброс счётчика через админку восстанавливает отключенного
    restored = await traffic.restore_after_limit_change(bot=fake_bot, ssh_fn=fake_ssh)
    # у 111-го лимит ещё 3 ГБ, расход 4 - не восстанавливается; поднимем
    assert restored == 0
    db.set_user_traffic_limit(111, sid, 3000)
    db.set_user_traffic_limit(222, sid, 3000)
    restored = await traffic.restore_after_limit_change(bot=fake_bot, ssh_fn=fake_ssh)
    assert restored == 1
    assert db.get_user_sub(111, sid)[5] == 1
    # предупреждение 222-го снято: до нового лимита далеко
    assert db.get_user_traffic(222, sid)[4] == 0


# ---------- ПЕРЕИСПОЛЬЗОВАНИЕ КОНФИГОВ ИЗ БАЗЫ ----------

@pytest.mark.asyncio
async def test_issue_reuses_saved_config_after_cut(fake_bot):
    """Срок истек/отключен по трафику - при повторной оплате пир возвращается
    ПРЕЖНИМИ ключами: старый .conf клиента продолжает работать."""
    sid = add_server("🇫🇮 Финляндия")
    db.add_or_update_user(111, sid, "user_111", days=30)
    db.set_user_config(111, sid, FAKE_CLIENT_CFG)
    db.apply_user_traffic_counters(111, sid, 3 * GB, 0)
    db.deactivate_user(111, sid)      # как после истечения срока
    db.mark_traffic_over(111, sid)    # или отключения по трафику

    fake_ssh = fake_ssh_factory(lambda cmd: "OK restored user_111 PUB" if "awg pubkey" in cmd
                                else f"неожиданная команда: {cmd}")
    import unittest.mock as _mock
    with _mock.patch("ssh_manager.run_ssh_command", fake_ssh):
        ok, msg = await user_handlers.issue_vpn_access(fake_bot, 111, sid, "30d")

    assert ok and msg == "Выдано (прежний конфиг)"
    # новый конфиг НЕ генерировался и НЕ рассылался
    assert not any("add_user.sh" in c for _i, _p, c in fake_ssh.calls)
    assert not any(kind in ("doc", "photo") for kind, *_ in fake_bot.sent)
    # конфиг в базе прежний, срок продлен, трафик обнулен (новый период оплаты)
    assert db.get_user_config(111, sid) == FAKE_CLIENT_CFG
    sub = db.get_user_sub(111, sid)
    assert sub[5] == 1
    tr = db.get_user_traffic(111, sid)
    assert tr[1] == 0 and tr[5] == 0
    # пользователю объяснено, что конфиг прежний
    assert any(kind == "msg" and chat == 111 and "прежний конфиг" in t
               for kind, chat, t, _k in fake_bot.sent)


@pytest.mark.asyncio
async def test_issue_falls_back_to_new_config_when_ip_taken(fake_bot):
    """Если прежний конфиг вернуть нельзя (его IP уже выдали другому) -
    генерируется новый, как раньше, и отправляется пользователю."""
    sid = add_server()
    db.add_or_update_user(111, sid, "user_111", days=30)
    db.set_user_config(111, sid, FAKE_CLIENT_CFG)
    db.deactivate_user(111, sid)

    def ssh_reply(cmd):
        if "awg pubkey" in cmd:
            return "Ошибка: IP занят другим пиром"
        if "add_user.sh" in cmd:
            return FAKE_CFG
        return "ok"

    fake_ssh = fake_ssh_factory(ssh_reply)
    import unittest.mock as _mock
    with _mock.patch("ssh_manager.run_ssh_command", fake_ssh):
        ok, msg = await user_handlers.issue_vpn_access(fake_bot, 111, sid, "30d")

    assert ok
    assert any("add_user.sh" in c for _i, _p, c in fake_ssh.calls)   # перевыдача
    assert any(kind == "doc" and chat == 111 for kind, chat, *_ in fake_bot.sent)  # новый файл
    assert db.get_user_config(111, sid) == FAKE_CFG                   # конфиг обновлен


def test_restore_cmd_has_duplicate_and_ip_guards():
    cmd = traffic.build_restore_peer_cmd("user_111", "PRIV", "PSK", "10.8.0.7")
    assert 'grep -c "# Peer: user_111"' in cmd        # не дублировать блок в конфиге
    assert "IP занят другим пиром" in cmd             # не отбирать IP у другого пира
    assert "AllowedIPs = 10.8.0.7/32" in cmd


def test_inactive_cleanup_retention_is_configurable():
    sid = add_server()
    db.add_or_update_user(111, sid, "user_111", days=30)
    db.deactivate_user(111, sid)
    conn = db.get_conn()
    conn.execute("UPDATE users SET last_expired='2020-01-01 00:00:00' WHERE tg_id=? AND server_id=?", (111, sid))
    conn.commit()
    conn.close()

    db.update_setting("inactive_cleanup_days", "0")   # хранить, пока числится в базе
    assert db.get_inactive_users_30d() == []
    db.update_setting("inactive_cleanup_days", "30")
    assert [u[0] for u in db.get_inactive_users_30d()] == [111]


# ---------- ЛИЧНЫЕ СООБЩЕНИЯ ИЗ АДМИНКИ ----------

def test_admin_can_message_each_user_from_users_page(admin_client, client, fake_bot):
    # страница отправки: контекст переписки + форма
    page = admin_client.get("/admin/web/users/message/111")
    assert page.status_code == 200
    assert "Сообщение пользователю" in page.text

    # отправка реальному пользователю: в чат (кабинет) + в Telegram
    r = admin_client.post("/admin/web/users/message",
                          data={"tg_id": 111, "text": "Здравствуйте! Вопрос по доступу решён."},
                          follow_redirects=True)
    assert "отправлено в Telegram" in r.text
    msgs = db.get_support_messages(111)
    assert [m[2] for m in msgs] == ["admin"]
    assert msgs[0][4] == "Здравствуйте! Вопрос по доступу решён."
    assert any(kind == "msg" and chat == 111 and "Сообщение от администратора" in t
               for kind, chat, t, _k in fake_bot.sent)

    # почтовый клиент (без Telegram): сообщение сохраняется в чате, TG не дёргается
    db.create_email_account("msg@example.com", None)
    v_tg = db.ensure_email_account_tg("msg@example.com")
    fake_bot.sent = []
    admin_client.post("/admin/web/users/message", data={"tg_id": v_tg, "text": "Для почтового клиента"})
    assert [m[2] for m in db.get_support_messages(v_tg)] == ["admin"]
    assert fake_bot.sent == []

    # пользователь видит сообщение в кабинете (чат поддержки)
    login(client, 111)
    history = client.get("/web/support/messages").json()["messages"]
    assert any("Вопрос по доступу решён." in m["text"] for m in history)

    # без прав админа - нельзя
    login(client, 555)
    assert client.post("/admin/web/users/message", data={"tg_id": 111, "text": "x"}).status_code == 403
    assert client.get("/admin/web/users/message/111").status_code == 403


# ---------- КАБИНЕТ И ОПИСАНИЕ ----------

def test_dashboard_shows_traffic_progress_and_over_banner(client):
    sid = add_server("🇳🇱 Нидерланды")
    login(client, 111)
    client.post("/web/accept_tos", follow_redirects=False)
    db.add_or_update_user(111, sid, "user_111", days=30)
    db.set_user_traffic_limit(111, sid, 5)
    db.apply_user_traffic_counters(111, sid, 1 * GB, 0)

    page = client.get("/dashboard")
    assert "📊 Трафик" in page.text
    assert "1.00 ГБ" in page.text and "5.00 ГБ" in page.text
    assert "Лимит трафика исчерпан" not in page.text  # еще не исчерпан

    # исчерпали -> баннер в кабинете (даже если подписка отключена)
    db.apply_user_traffic_counters(111, sid, 4 * GB + 1, 0)
    db.mark_traffic_over(111, sid)
    db.deactivate_user(111, sid)
    page = client.get("/dashboard")
    assert "Лимит трафика исчерпан" in page.text
    assert "Нидерланды" in page.text
    assert "Продлить доступ" in page.text


def test_service_description_mentions_traffic_limits(client):
    login(client, 111)
    client.post("/web/accept_tos", follow_redirects=False)
    dash = client.get("/dashboard").text
    assert "лимитом трафика" in dash
    assert "новый срок и свежий лимит" in dash

    home = client.get("/").text
    assert "лимита трафика" in home or "лимит трафика" in home.lower()
