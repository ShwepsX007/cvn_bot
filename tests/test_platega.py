"""Platega API и миграции: без реальных платежей и внешней сети."""

import asyncio
from unittest.mock import AsyncMock

import pytest

import database as db
import payments
import platega

pytestmark = pytest.mark.usefixtures("isolated_db")

MERCHANT_ID = "11111111-2222-4333-8444-555555555555"
TRANSACTION_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
SECRET = "test-platega-api-secret"


@pytest.fixture
def configured():
    db.update_settings({
        "payment_provider": "platega", "platega_merchant_id": MERCHANT_ID,
        "platega_secret": SECRET, "platega_payment_method": "auto",
        "site_url": "https://vpn.example.com",
    })


@pytest.fixture
def gateway(monkeypatch):
    def setup(body=None, status=200, error=None):
        calls = []

        class Response:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def json(self, **kwargs):
                if isinstance(body, Exception):
                    raise body
                return body

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def request(self, method, url, **kwargs):
                calls.append((method, url, kwargs))
                if error:
                    raise error
                response = Response()
                response.status = status
                return response

        monkeypatch.setattr(platega.aiohttp, "ClientSession", Session)
        return calls
    return setup


def test_fresh_defaults_have_credentials_and_auto_is_disabled():
    assert db.get_setting("platega_merchant_id") == ""
    assert db.get_setting("platega_secret") == ""
    assert db.get_setting("platega_payment_method") == "auto"
    assert db.get_setting("payment_provider") == "off"
    assert not payments.is_auto_pay_enabled()
    db.update_setting("payment_provider", "platega")
    assert not payments.is_auto_pay_enabled()
    with pytest.raises(payments.PaymentConfigurationError):
        payments.validate_configuration("platega")


def test_migration_preserves_old_settings_and_orders():
    db.update_settings({"platega_payment_method": "2", "platega_secret": SECRET})
    conn = db.get_conn()
    with conn:
        conn.execute("DROP TABLE orders")
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, tg_id INTEGER, server_id INTEGER, "
                     "period TEXT, amount INTEGER, status TEXT, created_at TIMESTAMP, aipay_uid TEXT)")
        conn.execute("INSERT INTO orders VALUES (1, 42, 5, '7d', 200, 'pending', CURRENT_TIMESTAMP, ?)", (TRANSACTION_ID,))
        conn.execute("DELETE FROM settings WHERE key='platega_merchant_id'")
    conn.close()
    db.init_db()
    db.init_db()
    assert db.get_setting("platega_payment_method") == "2"
    assert db.get_setting("platega_secret") == SECRET
    assert db.get_setting("platega_merchant_id") == ""
    assert db.get_order_by_uid(TRANSACTION_ID) == (1, 42, 5, "7d", 200, "pending")
    assert db.get_order_provider(TRANSACTION_ID) is None


@pytest.mark.parametrize("key,value", [
    ("platega_merchant_id", "not-a-uuid"), ("platega_merchant_id", ""),
    ("platega_secret", ""), ("platega_secret", "secret\nvalue"),
    ("platega_secret", "секрет"), ("platega_secret", "a b"),
    ("platega_payment_method", "-2"), ("platega_payment_method", "0"),
    ("platega_payment_method", "2.5"), ("platega_payment_method", "other"),
    ("platega_payment_method", ""), ("platega_payment_method", "9" * 100),
    ("platega_payment_method", "2147483648"),
    ("site_url", "http://example.com"), ("site_url", "https://example.com/dashboard"),
    ("site_url", "https://user:secret@example.com"), ("site_url", "https://example.com?x=1"),
    ("site_url", "https://example.com#part"), ("site_url", "https://example.com:wrong"),
    ("site_url", "https://exa mple.com"), ("site_url", "https://example.com?"),
    ("site_url", "https://example.com#"), ("site_url", "https://example.com:0"),
    ("site_url", "https://example.com\\path"), ("payment_provider", "unknown"),
])
def test_invalid_settings_are_rejected(key, value):
    with pytest.raises(ValueError):
        payments.normalize_setting(key, value)


def test_settings_normalization():
    assert platega.normalize_setting("platega_merchant_id", f" {MERCHANT_ID.upper()} ") == MERCHANT_ID
    assert platega.normalize_setting("platega_payment_method", "AUTO") == "auto"
    assert platega.normalize_setting("platega_payment_method", "02") == "2"
    assert platega.normalize_setting("site_url", "https://vpn.example.com/") == "https://vpn.example.com"
    assert platega.get_webhook_url("https://vpn.example.com/") == "https://vpn.example.com/webhook/platega"


@pytest.mark.parametrize("method,path,url_key", [
    ("auto", "/v2/transaction/process", "url"),
    ("2", "/transaction/process", "redirect"),
    ("13", "/transaction/process", "redirect"),
])
def test_create_order_payload_and_headers(configured, gateway, method, path, url_key):
    db.update_setting("platega_payment_method", method)
    calls = gateway({url_key: "https://pay.platega.io/?id=test", "transactionId": TRANSACTION_ID})
    result = asyncio.run(platega.create_order("200.125", tg_id=42, username="tester", client_ip="203.0.113.1"))
    assert result == ("https://pay.platega.io/?id=test", TRANSACTION_ID)
    assert len(calls) == 1
    verb, url, kwargs = calls[0]
    assert verb == "POST" and url == f"https://app.platega.io{path}"
    assert kwargs["headers"]["X-MerchantId"] == MERCHANT_ID
    assert kwargs["headers"]["X-Secret"] == SECRET
    assert kwargs["allow_redirects"] is False
    body = kwargs["json"]
    assert body["paymentDetails"] == {"amount": 200.13, "currency": "RUB"}
    assert body["return"] == body["failedUrl"] == "https://vpn.example.com/dashboard"
    assert body["metadata"] == {"userId": "42", "userName": "@tester", "clientIp": "203.0.113.1"}
    assert "id" not in body
    if method == "auto":
        assert "paymentMethod" not in body
    else:
        assert body["paymentMethod"] == int(method)


def test_email_only_account_metadata(configured, gateway):
    calls = gateway({"url": "https://pay.platega.io/", "transactionId": TRANSACTION_ID})
    virtual_id = db.make_virtual_tg_id(1)
    asyncio.run(platega.create_order(50, tg_id=virtual_id))
    assert calls[0][2]["json"]["metadata"] == {"userId": str(virtual_id), "userName": str(virtual_id)}


@pytest.mark.parametrize("amount", [0, -1, None, True, "nan", "inf", "0.001", "wrong"])
def test_invalid_amount_never_calls_api(configured, gateway, amount):
    calls = gateway({})
    with pytest.raises(platega.PlategaError, match="Сумма"):
        asyncio.run(platega.create_order(amount))
    assert not calls


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 302])
def test_http_errors_do_not_expose_raw_response_or_keys(configured, gateway, status):
    gateway({"message": SECRET, "redirect": "https://pay.platega.io/", "transactionId": TRANSACTION_ID}, status=status)
    with pytest.raises(platega.PlategaError) as exc:
        asyncio.run(platega.create_order(50))
    assert SECRET not in str(exc.value)
    assert MERCHANT_ID not in str(exc.value)


@pytest.mark.parametrize("body", [[], {}, ValueError("bad json"),
    {"url": "javascript:alert(1)", "transactionId": TRANSACTION_ID},
    {"url": "https://pay.platega.io/", "transactionId": "bad-id"},
])
def test_invalid_api_response(configured, gateway, body):
    gateway(body)
    with pytest.raises(platega.PlategaError):
        asyncio.run(platega.create_order(50))


def test_timeout_does_not_retry_creation(configured, gateway):
    calls = gateway(error=asyncio.TimeoutError())
    with pytest.raises(platega.PlategaError, match="Не удалось связаться"):
        asyncio.run(platega.create_order(50))
    assert len(calls) == 1


@pytest.mark.parametrize("raw,expected", [
    ("CONFIRMED", payments.STATUS_SUCCESS), ("canceled", payments.STATUS_CANCELLED),
    ("CHARGEBACKED", payments.STATUS_ERROR), ("PENDING", payments.STATUS_PENDING),
    ("unknown", payments.STATUS_PENDING), (None, payments.STATUS_PENDING),
])
def test_status_mapping(raw, expected):
    assert platega.map_status(raw) == expected


def test_status_uses_saved_provider_even_when_off(configured, gateway):
    calls = gateway({"status": "CONFIRMED", "paymentDetails": {"amount": 200, "currency": "RUB"}})
    db.create_auto_order(42, 5, "7d", 200, TRANSACTION_ID, "platega")
    db.update_setting("payment_provider", "off")
    status = asyncio.run(payments.get_order_status(TRANSACTION_ID, provider=db.get_order_provider(TRANSACTION_ID), expected_amount=200))
    assert status["id"] == payments.STATUS_SUCCESS
    assert calls[0][1] == f"{platega.BASE_URL}/transaction/{TRANSACTION_ID}"


@pytest.mark.parametrize("details", [{"amount": 199, "currency": "RUB"}, {"amount": 200, "currency": "USD"}, None])
def test_status_rejects_wrong_payment_details(configured, gateway, details):
    gateway({"status": "CONFIRMED", "paymentDetails": details})
    with pytest.raises(platega.PlategaError, match="Сумма или валюта"):
        asyncio.run(platega.get_order_status(TRANSACTION_ID, expected_amount=200))


def test_webhook_auth_requires_both_headers(configured):
    assert platega.verify_webhook_headers({"x-merchantid": MERCHANT_ID, "x-secret": SECRET})
    assert not platega.verify_webhook_headers({"X-MerchantId": MERCHANT_ID})
    assert not platega.verify_webhook_headers({"X-MerchantId": MERCHANT_ID, "X-Secret": "wrong"})
    assert not platega.verify_webhook_headers({"X-MerchantId": MERCHANT_ID, "X-Secret": "ключ"})
    db.update_setting("platega_secret", "")
    assert not platega.verify_webhook_headers({"X-MerchantId": MERCHANT_ID, "X-Secret": SECRET})


def test_disabled_creation_and_aipay_compatibility(monkeypatch):
    with pytest.raises(payments.AutoPayDisabled):
        asyncio.run(payments.create_order(50))
    db.update_settings({"payment_provider": "aipay", "aipay_api_key": "test-aipay-key"})
    create = AsyncMock(return_value=("https://aipay.example.com/", "old-uid"))
    monkeypatch.setattr(payments.aipay, "create_order", create)
    assert asyncio.run(payments.create_order(50, tg_id=42)) == ("https://aipay.example.com/", "old-uid")
    create.assert_awaited_once_with(50, "RUB")
    db.create_aipay_order(42, 5, "1d", 50, "old-uid")
    assert db.get_order_provider("old-uid") == "aipay"
