# -*- coding: utf-8 -*-
"""
Тесты независимой регистрации по почте:
  - почтовый аккаунт без Telegram получает виртуальный внутренний ID и свободно
    пользуется кабинетом (ToS, покупки, скачивание конфигов);
  - при привязке Telegram (виджет, вход через бота, ссылка-привязка из письма)
    почтовый аккаунт объединяется с Telegram-аккаунтом в один (данные переносятся);
  - привязка почты из Telegram-аккаунта продолжает работать.
Запуск: .testenv/bin/python -m pytest tests/test_email_account.py -q
"""
import hashlib
import hmac
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("BOT_TOKEN", "0:test")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import database as bot_db
import emailauth

# Изолируем БД на время всего pytest-прогона (рабочую базу не трогаем)
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
bot_db.DB_NAME = _tmp_db.name
bot_db.init_db()

import mailer  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import web_app  # noqa: E402

PASSWORD = "SuperSecret123"

# Перехватываем отправку писем: запоминаем (тип, получатель, токен).
SENT_MAILS = []


def _fake_send_token_mail(kind, to, token):
    SENT_MAILS.append((kind, to, token))
    return True, ""


mailer.send_token_mail = _fake_send_token_mail  # web_app/user_handlers смотрят атрибут модуля в момент вызова


@pytest.fixture(scope="session")
def client():
    # base_url https: сессионная кука выставляется с флагом Secure (https_only),
    # по "обычному" http тестовый клиент не стал бы её носить.
    with TestClient(web_app.app, base_url="https://testserver") as c:
        yield c


@pytest.fixture(autouse=True)
def clean_tables():
    conn = bot_db.get_conn()
    cur = conn.cursor()
    for t in ("email_accounts", "email_tokens", "tg_login_tokens", "users",
              "orders", "manual_orders", "detail_requests", "user_profiles", "servers"):
        cur.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()
    web_app.CAPTCHA_STORE.clear()
    SENT_MAILS.clear()
    yield


def add_paid_server(name="🌍 Тестовый сервер", ip="10.9.9.9"):
    bot_db.add_server(ip, name, 2222)
    conn = bot_db.get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM servers WHERE ip=?", (ip,))
    sid = cur.fetchone()[0]
    conn.close()
    bot_db.set_server_limit(sid, 10)
    return sid


def seed_captcha(answer=7):
    cap_id = "cap-test"
    web_app.CAPTCHA_STORE[cap_id] = {
        "answer": answer,
        "expires": datetime.utcnow() + timedelta(minutes=5),
        "fails": 0,
    }
    return cap_id, str(answer)


def register_email(client, email):
    cap_id, cap_ans = seed_captcha()
    return client.post("/auth/email/register", data={
        "email": email, "password": PASSWORD, "confirm": PASSWORD,
        "captcha_id": cap_id, "captcha_answer": cap_ans,
    }, follow_redirects=False)


def last_token(kind, email):
    tokens = [t for k, to, t in SENT_MAILS if k == kind and to == email]
    assert tokens, f"письмо {kind} для {email} не отправлялось"
    return tokens[-1]


def parse_db_date(s):
    """Даты из базы приходят с микросекундами или без."""
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f") if "." in s else datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def verify_email(client, email):
    return client.get(f"/auth/email/verify?token={last_token('verify', email)}")


def telegram_widget_params(tg_id, first_name="Тест", username=None):
    """Корректно подписанные параметры виджета 'Log in with Telegram'
    (подпись считается тем же способом, что проверяет webauth)."""
    params = {"id": str(tg_id), "first_name": first_name,
              "auth_date": str(int(time.time()))}
    if username:
        params["username"] = username
    check = "\n".join(f"{k}={params[k]}" for k in sorted(params))
    secret = hashlib.sha256(os.environ["BOT_TOKEN"].encode()).digest()
    params["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return params


# ======================= ВИРТУАЛЬНЫЕ ID И МЕЛКИЕ ФУНКЦИИ =======================

def test_virtual_id_helpers():
    vtg = bot_db.make_virtual_tg_id(42)
    assert bot_db.is_virtual_tg_id(vtg)
    assert bot_db.is_virtual_tg_id(str(vtg))
    assert not bot_db.is_virtual_tg_id(123456789)
    assert not bot_db.is_virtual_tg_id(None)
    assert not bot_db.is_virtual_tg_id("abc")
    assert not bot_db.is_virtual_tg_id(bot_db.VIRTUAL_TG_BASE * 2)
    assert bot_db.email_account_id_from_virtual(vtg) == 42
    assert bot_db.email_account_id_from_virtual(123456789) is None


def test_accept_tos_upsert_without_profile():
    # У почтовых аккаунтов строки профиля может не быть - accept_tos обязан создать её.
    bot_db.accept_tos(555000001)
    profile = bot_db.get_user_profile(555000001)
    assert profile and profile[3] == 1


def test_ensure_email_account_tg():
    bot_db.create_email_account("lazy@example.com", password_hash="x")
    tg1 = bot_db.ensure_email_account_tg("lazy@example.com")
    tg2 = bot_db.ensure_email_account_tg("lazy@example.com")
    assert tg1 == tg2 and bot_db.is_virtual_tg_id(tg1)
    # Повторный вызов не плодит новые ID
    assert bot_db.get_email_account("lazy@example.com")[3] == tg1


# ======================= ПОЛНЫЙ ЦИКЛ: РЕГИСТРАЦИЯ ПОЧТОЙ БЕЗ TELEGRAM =======================

def test_email_only_registration_full_cycle(client):
    email = "solo@example.com"

    r = register_email(client, email)
    assert r.status_code == 303 and "/login" in r.headers["location"]

    r = verify_email(client, email)
    assert r.status_code == 200

    acc = bot_db.get_email_account(email)
    assert acc[4] == 1, "почта должна быть подтверждена"
    vtg = acc[3]
    assert bot_db.is_virtual_tg_id(vtg), "почтовый аккаунт получает виртуальный внутренний ID"

    # Кабинет доступен БЕЗ Telegram: сначала экран правил...
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "Перед продолжением" in r.text

    # ...принимаем соглашение (профиль создается с нуля)...
    r = client.post("/web/accept_tos", follow_redirects=False)
    assert r.status_code == 303

    # ...и обычный кабинет с покупками.
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "Мои подписки" in r.text
    assert "Оформить / продлить доступ" in r.text
    # Кабинет предлагает привязать Telegram добровольно
    assert "вход по почте" in r.text

    # Повторный вход по почте в новой сессии
    client.cookies.clear()
    r = client.post("/auth/email/login", data={"email": email, "password": PASSWORD},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard"
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "Мои подписки" in r.text


def test_email_only_user_can_buy_and_download_config(client, monkeypatch):
    """Главный сценарий: зарегистрировался только по почте -> принял правила ->
    оформил доступ -> скачал конфиг. Телеграм-бот ни разу не понадобился."""
    sid = add_paid_server(ip="10.9.9.13")
    email = "buyer@example.com"

    register_email(client, email)
    verify_email(client, email)
    r = client.post("/web/accept_tos", follow_redirects=False)
    assert r.status_code == 303

    # Выдача доступа: подменяем только поход по SSH/в телеграм, база - настоящая
    issued = []

    async def fake_issue(bot, tg_id, server_id, period, notify_admin=False):
        issued.append((tg_id, server_id, period))
        bot_db.add_or_update_user(tg_id, server_id, f"user_{tg_id}", hours=24,
                                  is_trial=True, config_text="[Interface]\n# trial config")
        return True, "ok"

    monkeypatch.setattr(web_app, "issue_vpn_access", fake_issue)

    r = client.post("/web/order", json={"server_id": sid, "period": "trial", "method": "trial"})
    assert r.status_code == 200 and r.json().get("success")
    assert issued and bot_db.is_virtual_tg_id(issued[0][0]), "заказ оформлен на почтовый аккаунт"

    # Конфиг виден в кабинете и скачивается с сайта
    r = client.get("/dashboard")
    assert "Тестовый сервер" in r.text
    r = client.get(f"/download/{sid}")
    assert r.status_code == 200
    assert "# trial config" in r.text
    assert "buyer_AWG.conf" in r.headers.get("content-disposition", "")


def test_email_only_login_wrong_password(client):
    email = "wrongpw@example.com"
    register_email(client, email)
    verify_email(client, email)
    client.cookies.clear()
    r = client.post("/auth/email/login", data={"email": email, "password": "ДругойПароль1"},
                    follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]


# ======================= ОБЪЕДИНЕНИЕ АККАУНТОВ (ПОЧТА + TELEGRAM) =======================

def _make_email_only_account(email, server_id=None, days=7):
    """Регистрирует самостоятельный почтовый аккаунт; возвращает виртуальный ID."""
    bot_db.create_email_account(email, password_hash=emailauth.hash_password(PASSWORD))
    bot_db.mark_email_verified(email)
    vtg = bot_db.ensure_email_account_tg(email)
    if server_id:
        bot_db.add_or_update_user(vtg, server_id, f"user_{vtg}", days=days,
                                  config_text="[Interface]\n# test config")
        bot_db.create_order(vtg, server_id, f"{days}d", 200)
    return vtg


def test_merge_on_bot_login(client):
    """Вошли в Telegram через ссылку бота, будучи в почтовой сессии -> один аккаунт."""
    sid = add_paid_server()
    email = "merge1@example.com"
    vtg = _make_email_only_account(email, server_id=sid, days=7)

    # Сначала входим по почте (сессия запоминает почту) и принимаем соглашение
    r = client.post("/auth/email/login", data={"email": email, "password": PASSWORD},
                    follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/web/accept_tos", follow_redirects=False)
    assert r.status_code == 303

    # Затем вход по ссылке из бота -> аккаунты объединяются
    token = bot_db.create_tg_login_token(777000001)
    r = client.get(f"/auth/bot?token={token}", follow_redirects=False)
    assert r.status_code == 303

    # Принятое соглашение почтового аккаунта не потерялось при объединении
    profile = bot_db.get_user_profile(777000001)
    assert profile and profile[3] == 1

    # Подписки, заказы и почта переехали на реальный Telegram
    subs = bot_db.get_user_subs(777000001)
    assert len(subs) == 1 and subs[0][1] == sid
    assert bot_db.get_user_subs(vtg) == []
    assert bot_db.get_email_account(email)[3] == 777000001
    # В сессии теперь реальный tg - кабинет показывает подписку
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "Тестовый сервер" in r.text


def test_merge_on_telegram_widget(client):
    """Виджет 'Log in with Telegram' поверх почтовой сессии тоже объединяет аккаунты."""
    sid = add_paid_server(ip="10.9.9.10")
    email = "merge2@example.com"
    vtg = _make_email_only_account(email, server_id=sid)

    client.post("/auth/email/login", data={"email": email, "password": PASSWORD},
                follow_redirects=False)
    params = telegram_widget_params(777000002, first_name="Иван", username="ivan")
    r = client.get("/auth/telegram", params=params, follow_redirects=False)
    assert r.status_code in (302, 303, 307)

    assert len(bot_db.get_user_subs(777000002)) == 1
    assert bot_db.get_user_subs(vtg) == []
    assert bot_db.get_email_account(email)[3] == 777000002


def test_merge_on_link_letter(client):
    """Ссылка-привязка из письма (бот/кабинет) присоединяет почтовый аккаунт к tg."""
    sid = add_paid_server(ip="10.9.9.11")
    email = "merge3@example.com"
    vtg = _make_email_only_account(email, server_id=sid, days=3)

    token = bot_db.create_email_token(email, "link", 888000001, 7200)
    r = client.get(f"/auth/email/link?token={token}")
    assert r.status_code == 200
    assert "привязана" in r.text

    assert len(bot_db.get_user_subs(888000001)) == 1
    assert bot_db.get_user_subs(vtg) == []
    assert bot_db.get_email_account(email)[3] == 888000001


def test_merge_same_server_collision():
    """У обоих аккаунтов подписка на один сервер: побеждает более поздний срок."""
    sid = add_paid_server(ip="10.9.9.12")
    email = "merge4@example.com"
    bot_db.create_email_account(email, password_hash="x")
    vtg = bot_db.ensure_email_account_tg(email)
    real = 999000001

    bot_db.add_or_update_user(vtg, sid, f"user_{vtg}", days=10, config_text="v-cfg")
    bot_db.add_or_update_user(real, sid, f"user_{real}", days=1, config_text="r-cfg")

    assert bot_db.merge_virtual_into_real(vtg, real) is True
    subs = bot_db.get_user_subs(real)
    assert len(subs) == 1, "дублей подписок на один сервер быть не должно"
    exp = parse_db_date(subs[0][3])
    assert (exp - datetime.now()).days >= 9, "должна остаться более поздняя подписка"
    assert bot_db.get_user_config(real, sid) in ("v-cfg", "r-cfg")
    assert bot_db.get_user_subs(vtg) == []


def test_merge_rejects_bad_targets():
    bot_db.create_email_account("merge5@example.com", password_hash="x")
    vtg = bot_db.ensure_email_account_tg("merge5@example.com")
    assert bot_db.merge_virtual_into_real(vtg, vtg) is False          # цель виртуальная
    assert bot_db.merge_virtual_into_real(vtg, None) is False
    assert bot_db.merge_virtual_into_real(123456789, 999) is False    # источник не виртуальный


def test_tg_user_can_link_free_email_from_cabinet(client):
    """Telegram-аккаунт привязывает незанятую почту через карточку в кабинете."""
    # Вход через виджет
    params = telegram_widget_params(666000001, first_name="Пётр")
    r = client.get("/auth/telegram", params=params, follow_redirects=False)
    assert r.status_code in (302, 303, 307)

    r = client.post("/web/link_email", json={"email": "fresh@example.com"})
    assert r.status_code == 200 and r.json().get("ok")
    token = last_token("link", "fresh@example.com")
    r = client.get(f"/auth/email/link?token={token}")
    assert r.status_code == 200
    assert bot_db.get_email_account("fresh@example.com")[3] == 666000001


def test_cannot_steal_email_bound_to_other_telegram(client):
    """Почта уже привязана к другому РЕАЛЬНОМУ Telegram - привязка запрещена."""
    bot_db.create_email_account("taken@example.com", password_hash="x", tg_id=111222333, verified=1)

    params = telegram_widget_params(666000002, first_name="Вор")
    client.get("/auth/telegram", params=params, follow_redirects=False)
    r = client.post("/web/link_email", json={"email": "taken@example.com"})
    assert r.status_code == 409
    assert bot_db.get_email_account("taken@example.com")[3] == 111222333
