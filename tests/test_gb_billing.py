# -*- coding: utf-8 -*-
"""
Погигабайтная тарификация (billing_mode=traffic):

  - цена за ГБ и минимальная покупка из настроек, токен "10gb";
  - покупка ГБ: пополнение кошелька без изменения срока, переиспользование
    прежнего конфига, фолбэк на новый конфиг;
  - кошелек переживает сроковые покупки (INSERT OR REPLACE не сбрасывает баланс);
  - списание при расходе (пол в нуле), сроковая подписка гигабайты не жжет;
  - просроченные с балансом не отключаются (cleanup и фоновая задача);
  - отключение при исчерпании баланса и за неактивность (N месяцев);
  - заказ на сайте: минимальный порог, выдача;
  - остаток ГБ в кабинете.
"""

from unittest import mock

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
import base64
import json

import bot as bot_module
import database as db
import traffic
import user_handlers
import web_admin
import web_app

pytestmark = pytest.mark.usefixtures("isolated_db")

GB = 1024 ** 3

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


@pytest.fixture
def admin_client(monkeypatch, fake_bot):
    monkeypatch.setattr(web_app, "bot", fake_bot)
    monkeypatch.setattr(bot_module, "bot", fake_bot)
    with TestClient(web_app.app, base_url="https://testserver") as client:
        login(client, web_admin.ROOT_ADMIN_ID)
        yield client


def login(client, tg_id):
    client.cookies.clear()
    data = base64.b64encode(json.dumps({"tg_id": tg_id}).encode())
    client.cookies.set("vpn_session", TimestampSigner(web_app.SESSION_SECRET).sign(data).decode())


def add_server(name="🌍 Сервер", ip="10.9.9.9"):
    db.add_server(ip, name, 2222)
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM servers WHERE ip=?", (ip,))
    sid = cur.fetchone()[0]
    conn.close()
    return sid


def fake_ssh_factory(responses=None):
    calls = []

    async def fake_ssh(ip, port, cmd, **kw):
        calls.append((ip, port, cmd))
        if callable(responses):
            return responses(cmd)
        return responses if isinstance(responses, str) else "OK"

    fake_ssh.calls = calls
    return fake_ssh


def set_col(tg_id, sid, **cols):
    conn = db.get_conn()
    sets = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE users SET {sets} WHERE tg_id=? AND server_id=?",
                 (*cols.values(), tg_id, sid))
    conn.commit()
    conn.close()


def enable_gb_mode():
    db.update_setting("billing_mode", "traffic")


# ---------- ЦЕНА, ТОКЕНЫ, МИНИМАЛЬНАЯ ПОКУПКА ----------

def test_gb_token_pricing_and_labels():
    assert user_handlers.parse_gb_token("10gb") == 10
    assert user_handlers.parse_gb_token("30d") is None
    assert user_handlers.period_label("10gb") == "10 ГБ"
    assert user_handlers.period_label("30d") == "30 дней"

    sid = add_server()
    server = db.get_server_by_id(sid)
    db.update_setting("gb_price_rub", "75")
    assert user_handlers.get_price_for_period(server, "10gb") == 750
    # серверные цены периодов на гигабайты не влияют
    assert user_handlers.get_price_for_period(server, "1d") == int(db.get_setting("price_1d"))


@pytest.mark.asyncio
async def test_min_purchase_enforced(fake_bot):
    enable_gb_mode()
    db.update_setting("min_purchase_gb", "5")
    sid = add_server()
    ok, msg = await user_handlers.issue_vpn_access(fake_bot, 111, sid, "3gb")
    assert not ok and "Минимальная покупка" in msg
    assert db.get_user_balance(111, sid) == 0


# ---------- ПОКУПКА ГИГАБАЙТОВ ----------

@pytest.mark.asyncio
async def test_topup_for_active_sub_keeps_expire(fake_bot):
    """Активной подписке просто пополняем кошелек: срок и конфиг не трогаем."""
    enable_gb_mode()
    sid = add_server()
    db.add_or_update_user(111, sid, "user_111", days=30)
    expire_before = db.get_user_sub(111, sid)[3]

    fake_ssh = fake_ssh_factory("unexpected")
    with mock.patch("ssh_manager.run_ssh_command", fake_ssh):
        ok, msg = await user_handlers.issue_vpn_access(fake_bot, 111, sid, "10gb")

    assert ok and msg == "Гигабайты добавлены"
    assert fake_ssh.calls == []                      # на ноду ничего не ходили
    sub = db.get_user_sub(111, sid)
    assert sub[3] == expire_before                   # срок не изменился
    assert db.get_user_balance(111, sid) == 10
    assert db.get_user_gb(111, sid)[1] == 10         # всего куплено
    assert any(kind == "msg" and "Гигабайты куплены" in t
               for kind, chat, t, _k in fake_bot.sent)


@pytest.mark.asyncio
async def test_grant_gb_reuses_config_after_time_expires(fake_bot):
    """Срок вышел, конфиг сохранен - возвращаем ПРЕЖНИЙ пир, новый не генерируем."""
    enable_gb_mode()
    sid = add_server()
    db.add_or_update_user(903, sid, "user_903", days=1)
    db.set_user_config(903, sid, FAKE_CFG)
    set_col(903, sid, expire_date="2020-01-01 00:00:00", active=0)

    fake_ssh = fake_ssh_factory(lambda cmd: "OK restored user_903 PUB"
                                if "awg pubkey" in cmd else f"unexpected: {cmd}")
    with mock.patch("ssh_manager.run_ssh_command", fake_ssh):
        ok, msg = await user_handlers.issue_vpn_access(fake_bot, 903, sid, "5gb")

    assert ok and msg == "Выдано (прежний конфиг)"
    assert not any("add_user.sh" in c for _i, _p, c in fake_ssh.calls)
    assert db.get_user_config(903, sid) == FAKE_CFG
    assert db.get_user_balance(903, sid) == 5
    sub = db.get_user_sub(903, sid)
    assert sub[5] == 1                               # доступ возвращен
    assert sub[3].startswith("2020")                 # срок остался прежним (истекшим)
    assert any(kind == "msg" and "прежний конфиг" in t
               for kind, chat, t, _k in fake_bot.sent)


@pytest.mark.asyncio
async def test_grant_gb_new_config_fallback(fake_bot):
    """Конфига нет - генерируем новый и отправляем файл."""
    enable_gb_mode()
    sid = add_server()
    db.add_or_update_user(904, sid, "user_904", days=1)
    set_col(904, sid, expire_date="2020-01-01 00:00:00", active=0)

    fake_ssh = fake_ssh_factory(lambda cmd: FAKE_CFG if "add_user.sh" in cmd else "ok")
    with mock.patch("ssh_manager.run_ssh_command", fake_ssh):
        ok, msg = await user_handlers.issue_vpn_access(fake_bot, 904, sid, "5gb")

    assert ok and msg == "Выдано (гигабайты)"
    assert any("add_user.sh" in c for _i, _p, c in fake_ssh.calls)
    assert any(kind == "doc" and chat == 904 for kind, chat, *_ in fake_bot.sent)
    assert db.get_user_config(904, sid) == FAKE_CFG
    assert db.get_user_balance(904, sid) == 5


def test_balance_survives_time_purchase():
    """Сроковая покупка (INSERT OR REPLACE) не должна сбрасывать кошелек ГБ."""
    enable_gb_mode()
    sid = add_server()
    db.grant_gb(111, sid, "user_111", 10)
    db.add_or_update_user(111, sid, "user_111", days=30)
    assert db.get_user_balance(111, sid) == 10
    assert db.get_user_gb(111, sid)[1] == 10


# ---------- КОШЕЛЕК: СПИСАНИЕ ----------

def test_consume_balance_math_and_floor():
    enable_gb_mode()
    sid = add_server()
    db.grant_gb(111, sid, "user_111", 10)
    assert db.consume_gb_balance(111, sid, 2 * GB, burn=True) == 8
    assert db.consume_gb_balance(111, sid, 100 * GB, burn=True) == 0   # пол в нуле
    db.grant_gb(111, sid, "user_111", 5)
    assert db.consume_gb_balance(111, sid, 2 * GB, burn=False) == 5    # не списываем
    assert db.get_user_gb(111, sid)[2] is not None                     # активность отмечена


@pytest.mark.asyncio
async def test_time_mode_ignores_wallet():
    """В режиме по срокам кошелек инертен, просроченных отключаем как раньше."""
    sid = add_server()  # billing_mode = time (по умолчанию)
    db.grant_gb(111, sid, "user_111", 10)
    assert db.consume_gb_balance(111, sid, 2 * GB, burn=True) == 10    # не горит

    db.add_or_update_user(111, sid, "user_111", days=1)
    set_col(111, sid, expire_date="2020-01-01 00:00:00")

    fake_ssh = fake_ssh_factory("OK")
    with mock.patch("ssh_manager.run_ssh_command", fake_ssh):
        res = await user_handlers.check_and_clean_expired(111)
    assert res == "expired"
    assert db.get_user_sub(111, sid)[5] == 0


# ---------- ОТКЛЮЧЕНИЕ ПО СРОКУ / НЕАКТИВНОСТЬ / БАЛАНСУ ----------

@pytest.mark.asyncio
async def test_clean_expired_spares_gb_users(fake_bot):
    """ГБ-подписка с балансом переживает истечение срока (cleanup при заходе)."""
    enable_gb_mode()
    sid = add_server()
    db.grant_gb(111, sid, "user_111", 10)
    set_col(111, sid, expire_date="2020-01-01 00:00:00")

    fake_ssh = fake_ssh_factory("OK")
    with mock.patch("ssh_manager.run_ssh_command", fake_ssh):
        res = await user_handlers.check_and_clean_expired(111)
    assert res == "ok"
    assert db.get_user_sub(111, sid)[5] == 1

    # баланс кончился - честно отключаем
    db.consume_gb_balance(111, sid, 10 * GB, burn=True)
    with mock.patch("ssh_manager.run_ssh_command", fake_ssh):
        res = await user_handlers.check_and_clean_expired(111)
    assert res == "expired"
    assert db.get_user_sub(111, sid)[5] == 0


@pytest.mark.asyncio
async def test_expired_job_spares_gb_users(monkeypatch, fake_bot):
    """Фоновая задача отключения просроченных тоже щадит гигабайтные подписки."""
    enable_gb_mode()
    monkeypatch.setattr(bot_module, "bot", fake_bot)
    sid = add_server()
    db.grant_gb(901, sid, "user_901", 10)
    set_col(901, sid, expire_date="2020-01-01 00:00:00")

    fake_ssh = fake_ssh_factory("OK")
    with mock.patch("ssh_manager.run_ssh_command", fake_ssh):
        await bot_module.check_expired_users()
    assert not any("remove_user.sh" in c for _i, _p, c in fake_ssh.calls)
    assert db.get_user_sub(901, sid)[5] == 1

    db.consume_gb_balance(901, sid, 10 * GB, burn=True)
    with mock.patch("ssh_manager.run_ssh_command", fake_ssh):
        await bot_module.check_expired_users()
    assert any("remove_user.sh" in c for _i, _p, c in fake_ssh.calls)
    assert db.get_user_sub(901, sid)[5] == 0


@pytest.mark.asyncio
async def test_enforcement_cuts_when_gb_spent(monkeypatch, fake_bot):
    """Баланс исчерпан (срока нет) - доступ отключается с уведомлением."""
    enable_gb_mode()
    monkeypatch.setattr(bot_module, "bot", fake_bot)
    sid = add_server()
    db.grant_gb(901, sid, "user_901", 5)
    set_col(901, sid, expire_date="2020-01-01 00:00:00")
    db.consume_gb_balance(901, sid, 5 * GB, burn=True)

    fake_ssh = fake_ssh_factory(lambda cmd: "OK" if "remove_user.sh" in cmd else "ok")
    with mock.patch("ssh_manager.run_ssh_command", fake_ssh):
        await bot_module.check_traffic_limits()

    assert db.get_user_sub(901, sid)[5] == 0
    assert db.get_user_traffic(901, sid)[5] == 1      # traffic_over
    assert any(kind == "msg" and chat == 901 and "Трафик исчерпан" in t
               for kind, chat, t, _k in fake_bot.sent)


@pytest.mark.asyncio
async def test_enforcement_inactivity_cut_keeps_balance(monkeypatch, fake_bot):
    """N месяцев без расхода - отключение за неактивность, гигабайты сохраняются."""
    enable_gb_mode()
    db.update_setting("inactive_months_off", "3")
    monkeypatch.setattr(bot_module, "bot", fake_bot)
    sid = add_server()
    db.grant_gb(901, sid, "user_901", 10)
    set_col(901, sid, expire_date="2020-01-01 00:00:00",
            traffic_last_activity="2020-01-01 00:00:00")

    fake_ssh = fake_ssh_factory(lambda cmd: "OK" if "remove_user.sh" in cmd else "ok")
    with mock.patch("ssh_manager.run_ssh_command", fake_ssh):
        await bot_module.check_traffic_limits()

    assert db.get_user_sub(901, sid)[5] == 0
    assert db.get_user_balance(901, sid) == 10         # гигабайты не сгорели
    assert db.get_user_traffic(901, sid)[5] == 0       # это не «трафик исчерпан»
    assert any(kind == "msg" and chat == 901 and "неактивность" in t
               for kind, chat, t, _k in fake_bot.sent)


@pytest.mark.asyncio
async def test_enforcement_inactivity_off_and_time_valid_spared(monkeypatch, fake_bot):
    """inactive_months_off=0 - неактивность не режет; сроковая подписка
    с потраченным балансом тоже не режется (срок важнее)."""
    enable_gb_mode()
    db.update_setting("inactive_months_off", "0")
    monkeypatch.setattr(bot_module, "bot", fake_bot)
    sid = add_server()

    db.grant_gb(901, sid, "user_901", 10)
    set_col(901, sid, expire_date="2020-01-01 00:00:00",
            traffic_last_activity="2020-01-01 00:00:00")

    # срок еще действует, баланс израсходован - не отключать
    db.add_or_update_user(902, sid, "user_902", days=30)
    db.grant_gb(902, sid, "user_902", 10)
    db.consume_gb_balance(902, sid, 10 * GB, burn=True)
    assert db.get_user_balance(902, sid) == 0

    fake_ssh = fake_ssh_factory(lambda cmd: "OK" if "remove_user.sh" in cmd else "ok")
    with mock.patch("ssh_manager.run_ssh_command", fake_ssh):
        await bot_module.check_traffic_limits()

    assert db.get_user_sub(901, sid)[5] == 1           # неактивность выключена
    assert db.get_user_sub(902, sid)[5] == 1           # срок действует


@pytest.mark.asyncio
async def test_sync_burns_balance_only_without_time(monkeypatch):
    """Синхронизация жжет гигабайты только когда подписка живет по гигабайтам."""
    enable_gb_mode()
    sid = add_server()

    def dump_for(username, rx, tx):
        conf = (f"[Interface]\nPrivateKey = SP\n\n# Peer: {username}\n[Peer]\n"
                f"PublicKey = P\nAllowedIPs = 10.8.0.9/32\n")
        dump = f"P\tPSK\t1.1.1.1:1\t10.8.0.9/32\t1\t{rx}\t{tx}\t25"
        return f"---CONF---\n{conf}\n---DUMP---\n{dump}"

    # 901 живет по гигабайтам (срока нет) - 2 ГБ сжигают баланс
    db.grant_gb(901, sid, "user_901", 10)
    set_col(901, sid, expire_date="2020-01-01 00:00:00")
    fake_ssh = fake_ssh_factory(dump_for("user_901", 1 * GB, 1 * GB))
    await traffic.sync_all(ssh_fn=fake_ssh)
    assert abs(db.get_user_balance(901, sid) - 8.0) < 0.01

    # 902 на сроке - гигабайты в запасе не трогаем
    db.add_or_update_user(902, sid, "user_902", days=30)
    db.grant_gb(902, sid, "user_902", 10)
    fake_ssh = fake_ssh_factory(dump_for("user_902", 1 * GB, 1 * GB))
    await traffic.sync_all(ssh_fn=fake_ssh)
    assert db.get_user_balance(902, sid) == 10


# ---------- ЗАКАЗ НА САЙТЕ И КАБИНЕТ ----------

@pytest.mark.asyncio
async def test_web_order_gb_validation_and_request(client, fake_bot):
    enable_gb_mode()
    db.update_setting("min_purchase_gb", "5")
    db.update_setting("gb_price_rub", "50")
    sid = add_server()
    db.update_user_profile(42, "Test", "tester")
    db.accept_tos(42)
    login(client, 42)

    assert client.post("/web/order", json={"server_id": sid, "period": "3gb", "method": "manual"}).status_code == 400
    assert client.post("/web/order", json={"server_id": sid, "period": "abc", "method": "manual"}).status_code == 400

    r = client.post("/web/order", json={"server_id": sid, "period": "10gb", "method": "manual"})
    assert r.status_code == 200
    reqs = db.get_pending_detail_requests()
    assert len(reqs) == 1 and reqs[0][3] == "10gb"

    # подтверждение заявки админом выдает гигабайты (путь issue_vpn_access)
    r_id, r_tg, r_srv, r_period, _c = reqs[0]
    fake_ssh = fake_ssh_factory(lambda cmd: FAKE_CFG if "add_user.sh" in cmd else "ok")
    with mock.patch("ssh_manager.run_ssh_command", fake_ssh):
        ok, msg = await user_handlers.issue_vpn_access(FakeBot(), 42, sid, r_period)
    assert ok
    assert db.get_user_balance(42, sid) == 10


def test_dashboard_shows_gb_balance(client):
    enable_gb_mode()
    sid = add_server()
    db.update_user_profile(42, "Test", "tester")
    db.accept_tos(42)
    db.add_or_update_user(42, sid, "user_42", days=30)
    db.grant_gb(42, sid, "user_42", 10)

    login(client, 42)
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "Остаток трафика" in page.text
    assert "10.00 ГБ" in page.text
    assert "Купить гигабайты" in page.text or "гигабайты" in page.text


# ---------- АДМИНКА: РУЧНАЯ ВЫДАЧА ГИГАБАЙТОВ И ВИДИМОСТЬ БАЛАНСА ----------

def test_admin_grant_gb_any_amount(admin_client, fake_bot):
    """Админ выдает любое количество ГБ - минимальный порог покупки не действует."""
    enable_gb_mode()
    db.update_setting("min_purchase_gb", "5")
    sid = add_server()

    fake_ssh = fake_ssh_factory(lambda cmd: FAKE_CFG if "add_user.sh" in cmd else "ok")
    with mock.patch("ssh_manager.run_ssh_command", fake_ssh):
        r = admin_client.post("/admin/web/users/grant",
                              data={"tg_id": 777, "server_id": sid, "period": "gb", "gb_amount": "3"},
                              follow_redirects=True)
    assert r.status_code == 200
    assert "3 ГБ" in r.text                     # «Доступ (3 ГБ) выдан…»
    assert db.get_user_balance(777, sid) == 3   # меньше минимума - но выдано

    # ошибочные количества отклоняются
    r = admin_client.post("/admin/web/users/grant",
                          data={"tg_id": 777, "server_id": sid, "period": "gb", "gb_amount": "0"},
                          follow_redirects=True)
    assert "Укажите" in r.text or "должно быть" in r.text
    assert db.get_user_balance(777, sid) == 3   # не изменился


def test_admin_grant_periods_still_work(admin_client, fake_bot):
    """Сроковая выдача из админки работает и в гигабайтном режиме."""
    enable_gb_mode()
    sid = add_server()
    fake_ssh = fake_ssh_factory(lambda cmd: FAKE_CFG if "add_user.sh" in cmd else "ok")
    with mock.patch("ssh_manager.run_ssh_command", fake_ssh):
        r = admin_client.post("/admin/web/users/grant",
                              data={"tg_id": 778, "server_id": sid, "period": "7d", "gb_amount": ""},
                              follow_redirects=True)
    assert r.status_code == 200
    assert "7 дней" in r.text
    sub = db.get_user_sub(778, sid)
    assert sub and sub[5] == 1
    # срок установлен в будущем
    from user_handlers import parse_date
    assert parse_date(sub[3]).year >= 2026


def test_admin_pages_show_gb_balance(admin_client, fake_bot):
    """Баланс гигабайт виден на страницах «Клиенты» и «Трафик»."""
    enable_gb_mode()
    sid = add_server()
    db.update_user_profile(779, "Balance User", None)
    db.grant_gb(779, sid, "user_779", 10)
    db.consume_gb_balance(779, sid, 2.5 * GB, burn=True)

    page = admin_client.get("/admin/web/users")
    assert page.status_code == 200
    assert "7.50 ГБ" in page.text

    page = admin_client.get("/admin/web/traffic")
    assert page.status_code == 200
    assert "🪙 Гигабайты" in page.text
    assert "7.50 ГБ" in page.text
