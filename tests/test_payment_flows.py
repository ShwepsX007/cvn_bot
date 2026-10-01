"""Настройки, покупка, callback и ручная проверка Platega: реальные HTTP-ручки, фейковые API/SSH/TG."""

import asyncio
import base64
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

import admin_panel
import database as db
import payments
import platega
import user_handlers
import web_admin
import web_app

pytestmark = pytest.mark.usefixtures("isolated_db")

MERCHANT_ID = "11111111-2222-4333-8444-555555555555"
SECRET = "test-platega-api-secret"
UID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
HEADERS = {"X-MerchantId": MERCHANT_ID, "X-Secret": SECRET}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(web_app, "bot", SimpleNamespace(send_message=AsyncMock()))
    with TestClient(web_app.app, base_url="https://testserver") as client:
        yield client


def login(client, tg_id):
    client.cookies.clear()
    data = base64.b64encode(json.dumps({"tg_id": tg_id}).encode())
    client.cookies.set("vpn_session", TimestampSigner(web_app.SESSION_SECRET).sign(data).decode())


@pytest.fixture
def admin_client(client):
    login(client, web_admin.ROOT_ADMIN_ID)
    return client


@pytest.fixture
def configured():
    db.update_settings({"payment_provider": "platega", "platega_merchant_id": MERCHANT_ID,
                        "platega_secret": SECRET, "platega_payment_method": "auto",
                        "site_url": "https://vpn.example.com"})


def csrf(client):
    response = client.get("/admin/web/settings")
    assert response.status_code == 200
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text)[1]


def form_data(client, **changes):
    data = {"csrf_token": csrf(client), "payment_provider": "platega",
            "platega_merchant_id": MERCHANT_ID, "platega_secret": SECRET,
            "platega_payment_method": "auto", "site_url": "https://vpn.example.com/"}
    data.update(changes)
    return data


def test_empty_database_has_visible_payment_fields(admin_client):
    page = admin_client.get("/admin/web/settings")
    assert page.status_code == 200
    for name in ("platega_merchant_id", "platega_secret", "platega_payment_method", "payment_provider", "site_url"):
        assert page.text.count(f'name="{name}"') == 1
    assert 'type="password" name="platega_secret"' in page.text
    assert "https://amneziawg.fun/webhook/platega" in page.text
    assert "Callback URL" in page.text and 'value="auto"' in page.text


def test_save_all_settings_and_keep_empty_credentials(admin_client):
    response = admin_client.post("/admin/web/settings/payments", data=form_data(admin_client), follow_redirects=False)
    assert response.status_code == 303 and "msg=" in response.headers["location"]
    assert payments.is_auto_pay_enabled()
    assert db.get_setting("site_url") == "https://vpn.example.com"
    page = admin_client.get("/admin/web/settings")
    assert SECRET not in page.text and MERCHANT_ID not in page.text
    assert "https://vpn.example.com/webhook/platega" in page.text
    response = admin_client.post("/admin/web/settings/payments", data=form_data(
        admin_client, platega_merchant_id="", platega_secret="", platega_payment_method="2"
    ), follow_redirects=False)
    assert "msg=" in response.headers["location"]
    assert db.get_setting("platega_secret") == SECRET
    assert db.get_setting("platega_merchant_id") == MERCHANT_ID
    assert db.get_setting("platega_payment_method") == "2"


@pytest.mark.parametrize("changes", [
    {"platega_merchant_id": "bad-id"}, {"platega_secret": "a\nb"},
    {"platega_payment_method": "0"}, {"site_url": "http://vpn.example.com"},
    {"payment_provider": "unknown"}, {"platega_merchant_id": "", "platega_secret": ""},
])
def test_invalid_group_does_not_partially_save(admin_client, changes):
    old_settings = dict(db.get_all_settings())
    response = admin_client.post("/admin/web/settings/payments", data=form_data(admin_client, **changes), follow_redirects=False)
    assert response.status_code == 303
    assert "error" in parse_qs(urlsplit(response.headers["location"]).query)
    assert dict(db.get_all_settings()) == old_settings
    assert SECRET not in response.headers["location"]


def test_off_can_save_without_credentials(admin_client):
    response = admin_client.post("/admin/web/settings/payments", data=form_data(
        admin_client, payment_provider="off", platega_merchant_id="", platega_secret=""
    ), follow_redirects=False)
    assert "msg=" in response.headers["location"]
    assert not payments.is_auto_pay_enabled()


def test_settings_require_admin_and_csrf(client, admin_client):
    data = form_data(admin_client)
    data["csrf_token"] = "wrong"
    assert admin_client.post("/admin/web/settings/payments", data=data).status_code == 403
    assert admin_client.post("/admin/web/settings", data={"key": "payment_provider", "value": "off"}).status_code == 403
    login(client, 42)
    assert client.get("/admin/web/settings").status_code == 403
    assert client.post("/admin/web/settings/payments", data=data).status_code == 403
    client.cookies.clear()
    response = client.post("/admin/web/settings/payments", data=data, follow_redirects=False)
    assert response.headers["location"] == "/login"
    assert db.get_setting("payment_provider") == "off"


def test_generic_settings_cannot_bypass_provider_validation(admin_client):
    response = admin_client.post("/admin/web/settings", data={
        "key": "payment_provider", "value": "platega", "csrf_token": csrf(admin_client)
    }, follow_redirects=False)
    assert "error=" in response.headers["location"]
    assert db.get_setting("payment_provider") == "off"
    response = admin_client.post("/admin/web/settings", data={
        "key": "trial_hours", "value": "12", "csrf_token": csrf(admin_client)
    }, follow_redirects=False)
    assert "msg=" in response.headers["location"]
    assert db.get_setting("trial_hours") == "12"


def test_web_purchase_pins_provider_before_api_await(client, configured, monkeypatch):
    login(client, 42)
    db.update_user_profile(42, "Test", "tester")
    db.accept_tos(42)
    db.add_server("203.0.113.2", "Test server", 2222)
    sid = db.get_active_servers()[0][0]
    calls = []

    async def api(method, path, payload=None):
        calls.append((method, path, payload))
        # Админ выключил новые платежи, пока ожидали ответ API.
        db.update_setting("payment_provider", "off")
        return {"url": "https://pay.platega.io/", "transactionId": UID}

    monkeypatch.setattr(platega, "_request", api)
    response = client.post("/web/order", json={"server_id": sid, "period": "7d", "method": "auto"})
    assert response.status_code == 200 and response.json()["uid"] == UID
    assert db.get_order_provider(UID) == "platega"
    assert calls[0][1] == "/v2/transaction/process"
    assert calls[0][2]["metadata"]["userId"] == "42"
    assert calls[0][2]["metadata"]["userName"] == "@tester"
    assert not payments.is_auto_pay_enabled()
    assert client.post("/web/order", json={"server_id": sid, "period": "7d", "method": "auto"}).status_code == 503


def pending_order(provider="platega"):
    db.create_auto_order(42, 5, "7d", 200, UID, provider)


def callback_body(**changes):
    body = {"id": UID, "status": "CONFIRMED", "amount": 200, "currency": "RUB", "paymentMethod": 2}
    body.update(changes)
    return body


def test_callback_authenticated_and_idempotent(client, configured, monkeypatch):
    pending_order()
    issue = AsyncMock(return_value=(True, "ok"))
    monkeypatch.setattr(web_app, "issue_vpn_access", issue)
    db.update_setting("payment_provider", "off")
    assert client.post("/webhook/platega", json=callback_body()).status_code == 403
    for _ in range(2):
        assert client.post("/webhook/platega", headers=HEADERS, json=callback_body()).status_code == 200
    issue.assert_awaited_once_with(web_app.bot, 42, 5, "7d", notify_admin=True)
    assert db.get_order_by_uid(UID)[5] == "paid"


@pytest.mark.parametrize("changes", [{"amount": 199}, {"currency": "USD"}, {"amount": None}, {"amount": True}])
def test_callback_cannot_confirm_wrong_amount(client, configured, monkeypatch, changes):
    pending_order()
    issue = AsyncMock()
    monkeypatch.setattr(web_app, "issue_vpn_access", issue)
    assert client.post("/webhook/platega", headers=HEADERS, json=callback_body(**changes)).status_code == 400
    assert db.get_order_by_uid(UID)[5] == "pending"
    issue.assert_not_awaited()


@pytest.mark.parametrize("body", [[], {"id": UID}, {"id": {}, "status": "CONFIRMED"}, {"id": UID, "status": {}}])
def test_callback_rejects_malformed_json_object(client, configured, body):
    assert client.post("/webhook/platega", headers=HEADERS, json=body).status_code == 400


def test_unknown_callback_and_other_provider_are_ignored(client, configured, monkeypatch):
    issue = AsyncMock()
    monkeypatch.setattr(web_app, "issue_vpn_access", issue)
    assert client.post("/webhook/platega", headers=HEADERS, json=callback_body()).status_code == 200
    pending_order("aipay")
    assert client.post("/webhook/platega", headers=HEADERS, json=callback_body()).status_code == 200
    assert db.get_order_by_uid(UID)[5] == "pending"
    issue.assert_not_awaited()


@pytest.mark.parametrize("raw,expected", [("CANCELED", "cancelled"), ("CHARGEBACKED", "failed")])
def test_cancelled_callback_notifies_only_once(client, configured, raw, expected):
    pending_order()
    for _ in range(2):
        assert client.post("/webhook/platega", headers=HEADERS, json=callback_body(status=raw)).status_code == 200
    assert db.get_order_by_uid(UID)[5] == expected
    web_app.bot.send_message.assert_awaited_once()


def test_pending_callback_and_failed_fulfilment(client, configured, monkeypatch):
    pending_order()
    issue = AsyncMock(side_effect=RuntimeError("SSH unavailable"))
    monkeypatch.setattr(web_app, "issue_vpn_access", issue)
    assert client.post("/webhook/platega", headers=HEADERS, json=callback_body(status="PENDING")).status_code == 200
    issue.assert_not_awaited()
    assert client.post("/webhook/platega", headers=HEADERS, json=callback_body()).status_code == 200
    assert db.get_order_by_uid(UID)[5] == "paid"
    assert web_app.bot.send_message.await_args.args[0] == web_app.ADMIN_ID
    assert "SSH unavailable" in web_app.bot.send_message.await_args.args[1]


def test_polling_uses_order_provider_and_callback_does_not_grant_twice(client, configured, monkeypatch):
    pending_order()
    login(client, 42)
    db.update_setting("payment_provider", "aipay")
    api = AsyncMock(return_value={"status": "CONFIRMED", "paymentDetails": {"amount": 200, "currency": "RUB"}})
    issue = AsyncMock(return_value=(True, "ok"))
    monkeypatch.setattr(platega, "_request", api)
    monkeypatch.setattr(web_app, "issue_vpn_access", issue)
    assert client.get(f"/web/check_aipay/{UID}").json() == {"status": "paid"}
    api.assert_awaited_once_with("GET", f"/transaction/{UID}")
    assert client.post("/webhook/platega", headers=HEADERS, json=callback_body()).status_code == 200
    issue.assert_awaited_once()
    login(client, 99)
    assert client.get(f"/web/check_aipay/{UID}").status_code == 403


def fake_callback(data, tg_id=admin_panel.ADMIN_ID):
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=tg_id), answer=AsyncMock(),
                           message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()))


def test_telegram_provider_choice_validates_and_can_disable(configured):
    db.update_setting("platega_secret", "")
    db.update_setting("payment_provider", "off")
    callback = fake_callback("pay_provider_set_platega")
    asyncio.run(admin_panel.pay_provider_set(callback))
    assert db.get_setting("payment_provider") == "off"
    assert callback.answer.await_args.kwargs["show_alert"] is True
    db.update_setting("platega_secret", SECRET)
    asyncio.run(admin_panel.pay_provider_set(callback))
    assert db.get_setting("payment_provider") == "platega"
    callback.data = "pay_provider_set_off"
    asyncio.run(admin_panel.pay_provider_set(callback))
    assert db.get_setting("payment_provider") == "off"


def test_telegram_method_accepts_auto_and_rejects_zero():
    state = SimpleNamespace(clear=AsyncMock())
    message = SimpleNamespace(text="AUTO", answer=AsyncMock())
    asyncio.run(admin_panel.set_platega_method_proc(message, state))
    assert db.get_setting("platega_payment_method") == "auto"
    state.clear.reset_mock()
    message.text = "0"
    asyncio.run(admin_panel.set_platega_method_proc(message, state))
    state.clear.assert_not_awaited()
    assert db.get_setting("platega_payment_method") == "auto"


def test_telegram_cannot_check_other_users_payment(configured, monkeypatch):
    pending_order()
    status = AsyncMock()
    monkeypatch.setattr(payments, "get_order_status", status)
    callback = fake_callback(f"check_aipay_{UID}", tg_id=99)
    asyncio.run(user_handlers.autopay_check_status(callback))
    status.assert_not_awaited()
    assert "запрещён" in callback.answer.await_args.args[0]


def test_web_can_disable_even_misconfigured_provider(admin_client):
    db.update_settings({"payment_provider": "platega", "platega_payment_method": "broken", "site_url": "invalid"})
    page = admin_client.get("/admin/web/settings")
    assert "Выключить автооплату" in page.text
    response = admin_client.post("/admin/web/settings", data={
        "key": "payment_provider", "value": "off", "csrf_token": csrf(admin_client)
    }, follow_redirects=False)
    assert "msg=" in response.headers["location"]
    assert db.get_setting("payment_provider") == "off"


def test_telegram_purchase_stores_provider(configured, monkeypatch):
    db.add_server("203.0.113.3", "Test server", 2222)
    sid = db.get_active_servers()[0][0]
    api = AsyncMock(return_value={"url": "https://pay.platega.io/", "transactionId": UID})
    monkeypatch.setattr(platega, "_request", api)
    callback = fake_callback(f"autopay_{sid}_7d", tg_id=42)
    callback.from_user.username = "tester"
    asyncio.run(user_handlers.autopay_create(callback))
    assert db.get_order_provider(UID) == "platega"
    assert db.get_order_by_uid(UID)[1:5] == (42, sid, "7d", 200)
    assert api.await_args.args[1] == "/v2/transaction/process"


def test_telegram_polling_passes_saved_provider_and_amount(configured, monkeypatch):
    pending_order()
    db.update_setting("payment_provider", "off")
    remote = AsyncMock(return_value={"id": payments.STATUS_SUCCESS, "name": "CONFIRMED"})
    issue = AsyncMock(return_value=(True, "ok"))
    monkeypatch.setattr(payments, "get_order_status", remote)
    monkeypatch.setattr(user_handlers, "issue_vpn_access", issue)
    callback = fake_callback(f"check_aipay_{UID}", tg_id=42)
    callback.bot = SimpleNamespace()
    asyncio.run(user_handlers.autopay_check_status(callback))
    remote.assert_awaited_once_with(UID, provider="platega", expected_amount=200)
    issue.assert_awaited_once_with(callback.bot, 42, 5, "7d", notify_admin=True)
    assert db.get_order_by_uid(UID)[5] == "paid"


def test_telegram_secret_is_not_echoed_and_message_is_deleted():
    state = SimpleNamespace(clear=AsyncMock())
    message = SimpleNamespace(text=SECRET, answer=AsyncMock(), delete=AsyncMock())
    asyncio.run(admin_panel.set_platega_secret_proc(message, state))
    assert db.get_setting("platega_secret") == SECRET
    assert SECRET not in message.answer.await_args.args[0]
    message.delete.assert_awaited_once()


def test_admin_router_rejects_client_callbacks_and_messages():
    client_event = SimpleNamespace(from_user=SimpleNamespace(id=42))
    root_event = SimpleNamespace(from_user=SimpleNamespace(id=admin_panel.ADMIN_ID))
    for observer in (admin_panel.admin_router.callback_query, admin_panel.admin_router.message):
        assert asyncio.run(observer.check_root_filters(client_event))[0] is False
        assert asyncio.run(observer.check_root_filters(root_event))[0] is True


def test_web_polling_does_not_grant_for_wrong_remote_amount(client, configured, monkeypatch):
    pending_order()
    login(client, 42)
    api = AsyncMock(return_value={"status": "CONFIRMED", "paymentDetails": {"amount": 100, "currency": "RUB"}})
    issue = AsyncMock()
    monkeypatch.setattr(platega, "_request", api)
    monkeypatch.setattr(web_app, "issue_vpn_access", issue)
    assert client.get(f"/web/check_aipay/{UID}").json() == {"status": "pending"}
    assert db.get_order_by_uid(UID)[5] == "pending"
    issue.assert_not_awaited()
