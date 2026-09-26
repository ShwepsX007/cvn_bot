# -*- coding: utf-8 -*-
"""
Тесты бесплатного доступа на сайте (/free → /free/config → продление/скачивание/QR).
Тяжелые побочки (SSH на нодах, Telegram-бот) подменяются фейками.
Запуск: .testenv/bin/python -m pytest tests/test_free_pages.py -q
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("BOT_TOKEN", "0:test")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import database as bot_db

# Изолируем БД на время всего pytest-прогона (рабочую базу не трогаем)
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
bot_db.DB_NAME = _tmp_db.name
bot_db.init_db()  # создать таблицы уже в изолированном файле

from fastapi.testclient import TestClient
import web_app


@pytest.fixture(scope="session")
def client():
    with TestClient(web_app.app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_free_tables():
    conn = bot_db.get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM free_accesses")
    cur.execute("DELETE FROM servers")
    conn.commit()
    conn.close()
    web_app.CAPTCHA_STORE.clear()
    yield


def add_free_server(name="🎁 Тест FREE", max_users=2):
    bot_db.add_server("10.0.0.1", name, 2222)
    conn = bot_db.get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM servers WHERE ip = '10.0.0.1'")
    sid = cur.fetchone()[0]
    conn.close()
    bot_db.set_server_free(sid, True)
    bot_db.set_server_limit(sid, max_users)
    return sid


def live_expires(hours=3):
    return (datetime.utcnow() + timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')


# ---------- МОНКЕЙПАТЧИ ПОБОЧЕК ----------

FAKE_CFG = "[Interface]\nPrivateKey = AAA\nEndpoint = 10.0.0.1:51820\n"

async def _fake_create_peer(ip, port):
    return FAKE_CFG, "fake_peer_1"

async def _fake_delete_peer(ip, port, peer):
    web_app.TEST_DELETED.append((ip, port, peer))

class _FakeBot:
    sent = []
    async def send_message(self, tg_id, text, **kw):
        _FakeBot.sent.append((tg_id, text))
        return None


@pytest.fixture(autouse=True)
def patch_side_effects(monkeypatch):
    web_app.TEST_DELETED = []
    monkeypatch.setattr(web_app, "create_amnezia_peer", _fake_create_peer)
    monkeypatch.setattr(web_app, "delete_amnezia_peer", _fake_delete_peer)
    monkeypatch.setattr(web_app, "bot", _FakeBot())
    _FakeBot.sent = []
    yield


def get_captcha(client):
    r = client.get("/api/captcha")
    assert r.status_code == 200
    data = r.json()
    answer = web_app.CAPTCHA_STORE[data["captcha_id"]]["answer"]
    return data["captcha_id"], str(answer)


# ---------- ТЕСТЫ УТИЛИТ БД ----------

def test_db_flag_and_lists():
    sid = add_free_server()
    paid = bot_db.get_active_servers()        # без бесплатных
    free = bot_db.get_active_free_servers()
    assert all(s[0] != sid for s in paid)
    assert [s[0] for s in free] == [sid]
    assert [s[0] for s in bot_db.get_active_servers(include_free=True)] == [sid]
    assert bot_db.get_server_free_flags() == {sid: True}

    bot_db.set_server_free(sid, False)
    conn = bot_db.get_conn(); cur = conn.cursor()
    cur.execute("SELECT is_free FROM servers WHERE id=?", (sid,))
    assert cur.fetchone()[0] in (0, None)
    conn.close()
    bot_db.set_server_free(sid, True)


def test_db_access_lifecycle():
    sid = add_free_server()
    exp = live_expires(3)
    aid = bot_db.free_create_access("1.2.3.4", sid, "peer_9", FAKE_CFG, exp, tg_id=123, download_token="tok1")
    assert bot_db.free_get_active_by_ip("1.2.3.4")[0] == aid
    assert bot_db.free_get_active_by_ip("8.8.8.8") is None

    new_exp = live_expires(6)
    bot_db.free_renew_access(aid, new_exp)
    row = bot_db.free_get_by_token("tok1")
    assert row and row[4] == new_exp

    bot_db.free_deactivate(aid)
    assert bot_db.free_get_active_by_ip("1.2.3.4") is None


def test_db_slots_and_expired():
    sid = add_free_server(max_users=2)
    exp = live_expires(3)
    bot_db.free_create_access("1.1.1.1", sid, "p1", FAKE_CFG, exp)
    bot_db.free_create_access("2.2.2.2", sid, "p2", FAKE_CFG, exp)
    assert bot_db.free_count_active_on_server(sid) == 2

    past = (datetime.utcnow() - timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
    aid3 = bot_db.free_create_access("3.3.3.3", sid, "p3", FAKE_CFG, past)
    expired = bot_db.free_get_expired()
    assert any(e[0] == aid3 for e in expired)


# ---------- ТЕСТЫ СТРАНИЦ ----------

def test_free_page_renders(client):
    r = client.get("/free")
    assert r.status_code == 200
    assert "Бесплатный доступ" in r.text


def test_legal_documents_are_separate_and_public(client):
    terms = client.get("/terms")
    privacy = client.get("/privacy")
    home = client.get("/")

    assert terms.status_code == privacy.status_code == home.status_code == 200
    assert '<h1 class="h3 fw-bold mb-4">Пользовательское соглашение</h1>' in terms.text
    assert '<h1 class="h3 fw-bold mb-4">Политика конфиденциальности</h1>' in privacy.text
    assert 'href="/terms"' in home.text
    assert 'href="/privacy"' in home.text

    public_pages = home.text + client.get("/free").text + terms.text + privacy.text
    assert "VPN" not in public_pages.upper()
    assert "Блокиров" not in public_pages
    assert "Обход" not in public_pages


def test_config_page_grant_mode_with_server(client):
    add_free_server()
    r = client.get("/free/config")
    assert r.status_code == 200
    assert "Тест FREE" in r.text          # сервер предлагается
    assert "Получить конфиг" in r.text


def test_grant_flow_and_renew(client):
    sid = add_free_server()
    cap_id, cap_ans = get_captcha(client)
    r = client.post("/free/config", data={
        "action": "grant", "server_id": sid,
        "captcha_id": cap_id, "captcha_answer": cap_ans,
    }, follow_redirects=False)
    assert r.status_code == 303
    assert "msg=" in r.headers["location"]

    # в БД есть активная запись, конфиг тестовый
    acc = bot_db.free_get_active_by_ip("testclient")
    if not acc:
        # TestClient заполняет request.client.host как "testclient"
        conn = bot_db.get_conn(); cur = conn.cursor()
        cur.execute("SELECT ip FROM free_accesses WHERE active=1")
        rows = cur.fetchall(); conn.close()
        assert rows
        acc = bot_db.free_get_active_by_ip(rows[0][0])
    assert acc is not None
    # (id, server_id, username, config_text, expires_at, active, notified, tg_id, download_token)
    acc_id, server_id, cfg, exp1, tok = acc[0], acc[1], acc[3], acc[4], acc[8]
    assert server_id == sid
    assert cfg == FAKE_CFG

    # повторная выдача пока активна — отказ
    cap_id, cap_ans = get_captcha(client)
    r2 = client.post("/free/config", data={
        "action": "grant", "server_id": sid,
        "captcha_id": cap_id, "captcha_answer": cap_ans,
    }, follow_redirects=False)
    assert "error=" in r2.headers["location"]

    # скачивание по токену
    r3 = client.get(f"/free/download/{tok}")
    assert r3.status_code == 200
    assert r3.content.decode() == FAKE_CFG
    assert f"filename=free{acc_id}.conf" in r3.headers["content-disposition"]

    # QR работает
    r4 = client.get(f"/free/qr/{tok}")
    assert r4.status_code == 200
    assert r4.headers["content-type"] == "image/png"

    # продление сдвинула срок и сбросила notified.
    # (чтобы сравнение дат было строгим, искусственно отматываем срок на час назад)
    shorter = live_expires(2)
    conn = bot_db.get_conn(); cur = conn.cursor()
    cur.execute("UPDATE free_accesses SET notified=1, expires_at=? WHERE id=?", (shorter, acc_id))
    conn.commit(); conn.close()
    cap_id, cap_ans = get_captcha(client)
    r5 = client.post("/free/config", data={
        "action": "renew", "captcha_id": cap_id, "captcha_answer": cap_ans,
    }, follow_redirects=False)
    assert "msg=" in r5.headers["location"]
    acc2 = bot_db.free_get_by_token(tok)
    assert acc2[4] > shorter
    conn = bot_db.get_conn(); cur = conn.cursor()
    cur.execute("SELECT notified FROM free_accesses WHERE id=?", (acc_id,))
    assert cur.fetchone()[0] == 0
    conn.close()


def test_wrong_captcha_rejected(client):
    add_free_server()
    r = client.post("/free/config", data={
        "action": "grant", "server_id": 1,
        "captcha_id": "no_such", "captcha_answer": "1",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


def test_slots_full_unavailable(client):
    sid = add_free_server(max_users=1)
    cap_id, cap_ans = get_captcha(client)
    client.post("/free/config", data={
        "action": "grant", "server_id": sid,
        "captcha_id": cap_id, "captcha_answer": cap_ans,
    })
    # слотов нет -> страница показывает "мест нет"
    r = client.get("/free/config")  # тот же IP уже держит активную -> mode=active
    assert "Скачать" in r.text
    # другой IP видит заполненную ноду
    client2_headers = {"X-Real-IP": "9.9.9.9"}
    r2 = client.get("/free/config", headers=client2_headers)
    assert r2.status_code == 200
    assert "мест нет" in r2.text


# ---------- ФОНОВЫЕ ДЖОБЫ ----------

@pytest.mark.asyncio
async def test_expired_job_disconnects_and_deactivates_anyio():
    sid = add_free_server()
    past = (datetime.utcnow() - timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
    aid = bot_db.free_create_access("5.5.5.5", sid, "peer_exp", FAKE_CFG, past)
    await web_app.check_expired_free_accesses()
    assert "peer_exp" in [p[2] for p in web_app.TEST_DELETED]
    assert bot_db.free_get_active_by_ip("5.5.5.5") is None


@pytest.mark.asyncio
async def test_reminder_job_sends_telegram_anyio(monkeypatch):
    sid = add_free_server()
    soon = (datetime.utcnow() + timedelta(minutes=20)).strftime('%Y-%m-%d %H:%M:%S')
    bot_db.update_setting("free_remind_minutes", "30")
    aid = bot_db.free_create_access("6.6.6.6", sid, "peer_soon", FAKE_CFG, soon, tg_id=777, download_token="tokx")

    await web_app.free_reminders_job()
    assert any(tg == 777 for tg, _ in _FakeBot.sent)
    conn = bot_db.get_conn(); cur = conn.cursor()
    cur.execute("SELECT notified FROM free_accesses WHERE id=?", (aid,))
    assert cur.fetchone()[0] == 1
    conn.close()
    # в тексте напоминания есть ссылка на сайт
    assert "/free" in _FakeBot.sent[0][1]

    # повторно не шлёт
    _FakeBot.sent = []
    await web_app.free_reminders_job()
    assert _FakeBot.sent == []
