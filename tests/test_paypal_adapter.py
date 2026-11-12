"""PayPal Orders v2 adapter tests. Everything goes through ``httpx.MockTransport``: no real PayPal, no .env."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import threading
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from adapters.paypal.adapter import SANDBOX_BASE_URL, PayPalAdapter, is_sandbox_base_url
from pact_core.config import Settings
from pact_core.ports import CreatePayment, PaymentAdapter, ProviderError, ProviderOrder, ProviderTimeout

CLIENT_ID = "test-client-id"
CLIENT_SECRET = "s3cr3t-CLIENT-value-do-not-leak"
TOKEN_1 = "A21AA-access-token-one-do-not-leak"
TOKEN_2 = "A21AA-access-token-two-do-not-leak"
ORDER_ID = "5O190127TN364715T"


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakePayPal:
    """Records requests; routes them to per-path handlers that tests override."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.tokens = [TOKEN_1, TOKEN_2]
        self.token_calls = 0
        self.expires_in = 32400
        self.handlers: dict[tuple[str, str], object] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key in self.handlers:
            h = self.handlers[key]
            return h(request) if callable(h) else h
        if key == ("POST", "/v1/oauth2/token"):
            tok = self.tokens[min(self.token_calls, len(self.tokens) - 1)]
            self.token_calls += 1
            return httpx.Response(200, json={"scope": "x", "access_token": tok, "token_type": "Bearer",
                                             "app_id": "APP-1", "expires_in": self.expires_in, "nonce": "n"})
        return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND", "message": "no route",
                                         "details": [{"issue": "INVALID_RESOURCE_ID"}]})

    def api_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path != "/v1/oauth2/token"]


@pytest.fixture
def fake() -> FakePayPal:
    return FakePayPal()


@pytest.fixture
def clock() -> FakeClock:  # overrides the conftest datetime clock for this module
    return FakeClock()


@pytest.fixture
def adapter(fake, clock) -> PayPalAdapter:
    a = PayPalAdapter(CLIENT_ID, CLIENT_SECRET, transport=httpx.MockTransport(fake), clock=clock)
    yield a
    a.close()


def _req(**overrides) -> CreatePayment:
    base = dict(reference_id="req_abc123", merchant_order_id="ord_1", amount_minor=4200, currency="USD",
                description="Mechanical keyboard", return_url="http://localhost:8000/payments/return",
                cancel_url="http://localhost:8000/payments/cancel", idempotency_key="pact-req_abc123-create")
    base.update(overrides)
    return CreatePayment(**base)


def _created_body(order_id: str = ORDER_ID) -> dict:
    return {
        "id": order_id,
        "status": "PAYER_ACTION_REQUIRED",
        "payment_source": {"paypal": {}},
        "purchase_units": [{"reference_id": "req_abc123", "custom_id": "req_abc123", "invoice_id": "req_abc123",
                            "amount": {"currency_code": "USD", "value": "42.00"}}],
        "links": [
            {"href": f"https://api.sandbox.paypal.com/v2/checkout/orders/{order_id}", "rel": "self", "method": "GET"},
            {"href": f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}", "rel": "payer-action",
             "method": "GET"},
        ],
    }


def _captured_body(status: str = "COMPLETED", value: str = "42.00", reason: str | None = None) -> dict:
    capture = {"id": "3C679366HH908993F", "status": status,
               "amount": {"currency_code": "USD", "value": value}, "final_capture": True}
    if reason:
        capture["status_details"] = {"reason": reason}
    return {
        "id": ORDER_ID,
        "status": "COMPLETED",
        "purchase_units": [{"reference_id": "req_abc123", "custom_id": "req_abc123",
                            "amount": {"currency_code": "USD", "value": value},
                            "payments": {"captures": [capture]}}],
        "payer": {"name": {"given_name": "John"}, "email_address": "buyer@example.com"},
        "links": [{"href": "https://api.sandbox.paypal.com/v2/checkout/orders/x", "rel": "self", "method": "GET"}],
    }


def _error(status: int, name: str, issue: str | None = None, description: str = "desc") -> httpx.Response:
    body = {"name": name, "message": "The requested action could not be performed.", "debug_id": "f1e2d3c4"}
    if issue:
        body["details"] = [{"issue": issue, "description": description}]
    return httpx.Response(status, json=body)


# ----------------------------------------------------------------------------- protocol & construction


def test_implements_payment_adapter_protocol(adapter):
    port: PaymentAdapter = adapter
    assert port.name == "paypal"


def test_accepts_pydantic_secretstr(fake, clock):
    a = PayPalAdapter(CLIENT_ID, SecretStr(CLIENT_SECRET), transport=httpx.MockTransport(fake), clock=clock)
    a.verify_credentials()
    auth = fake.requests[0].headers["Authorization"]
    assert base64.b64decode(auth.split()[1]).decode() == f"{CLIENT_ID}:{CLIENT_SECRET}"


@pytest.mark.parametrize("cid,secret", [("", CLIENT_SECRET), (CLIENT_ID, ""), (CLIENT_ID, SecretStr(""))])
def test_requires_credentials(cid, secret):
    with pytest.raises(ValueError):
        PayPalAdapter(cid, secret)


# ----------------------------------------------------------------------------- sandbox-only base URLs

ALLOWED_URLS = [
    SANDBOX_BASE_URL,
    "https://api-m.sandbox.paypal.com/",
    "https://api.sandbox.paypal.com",
    "http://localhost:8080",
    "http://127.0.0.1:9000",
    "http://[::1]:9000",
    "http://testserver",
    "http://paypal.localhost",
    "https://mock-paypal.test",
]
REJECTED_URLS = [
    "https://api-m.paypal.com",
    "http://api-m.paypal.com",  # the old check accepted any http:// URL
    "https://api.paypal.com",
    "http://api-m.sandbox.paypal.com",  # sandbox must be https
    "https://sandboxpaypal.com",  # 'sandbox' must be a whole host label
    "https://api-m.paypal.com/sandbox",  # ... of the host, not the path
    "https://notsandbox.paypal.com",
    "ftp://localhost",
    "https://user:pass@api-m.sandbox.paypal.com",  # embedded credentials
    "localhost:8080",
    "",
]


@pytest.mark.parametrize("url", ALLOWED_URLS)
def test_sandbox_and_local_base_urls_allowed(url):
    assert is_sandbox_base_url(url)
    PayPalAdapter(CLIENT_ID, CLIENT_SECRET, base_url=url).close()


@pytest.mark.parametrize("url", REJECTED_URLS)
def test_live_and_malformed_base_urls_rejected(url):
    assert not is_sandbox_base_url(url)
    with pytest.raises(ValueError) as exc:
        PayPalAdapter(CLIENT_ID, CLIENT_SECRET, base_url=url)
    assert "pass" not in str(exc.value) and (not url or url not in str(exc.value))


@pytest.mark.parametrize("url", [u for u in ALLOWED_URLS + REJECTED_URLS if "@" not in u])
def test_base_url_rule_matches_config(url, monkeypatch):
    """The adapter enforces the same rule as Settings.paypal_base_url (except it also refuses userinfo)."""
    for name in list(os.environ):
        if name.startswith(("PACT_", "PAYPAL_")):
            monkeypatch.delenv(name)
    try:
        Settings(_env_file=None, PAYPAL_BASE_URL=url)
        config_ok = True
    except ValueError:
        config_ok = False
    assert is_sandbox_base_url(url) == config_ok


# ----------------------------------------------------------------------------- OAuth


def test_oauth_request_shape(adapter, fake):
    adapter.verify_credentials()
    (r,) = fake.requests
    assert r.method == "POST" and str(r.url) == f"{SANDBOX_BASE_URL}/v1/oauth2/token"
    scheme, cred = r.headers["Authorization"].split()
    assert scheme == "Basic"
    assert base64.b64decode(cred).decode() == f"{CLIENT_ID}:{CLIENT_SECRET}"
    assert r.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert parse_qs(r.content.decode()) == {"grant_type": ["client_credentials"]}


def test_token_cached_across_calls(adapter, fake):
    fake.handlers[("GET", f"/v2/checkout/orders/{ORDER_ID}")] = httpx.Response(200, json=_created_body())
    for _ in range(3):
        adapter.get_order(ORDER_ID)
    assert fake.token_calls == 1
    assert all(r.headers["Authorization"] == f"Bearer {TOKEN_1}" for r in fake.api_requests())


def test_token_refreshed_60s_before_expiry(adapter, fake, clock):
    fake.expires_in = 3600
    fake.handlers[("GET", f"/v2/checkout/orders/{ORDER_ID}")] = httpx.Response(200, json=_created_body())
    adapter.get_order(ORDER_ID)
    clock.t += 3600 - 60 - 1  # still inside the validity window
    adapter.get_order(ORDER_ID)
    assert fake.token_calls == 1
    clock.t += 1  # reached expires_in - 60s
    adapter.get_order(ORDER_ID)
    assert fake.token_calls == 2
    assert fake.api_requests()[-1].headers["Authorization"] == f"Bearer {TOKEN_2}"


def test_short_lived_token_still_cached(adapter, fake, clock):
    fake.expires_in = 30
    adapter.verify_credentials()
    clock.t += 10
    adapter.verify_credentials()
    assert fake.token_calls == 1
    clock.t += 5
    adapter.verify_credentials()
    assert fake.token_calls == 2


def test_token_fetch_is_thread_safe(adapter, fake):
    fake.handlers[("GET", f"/v2/checkout/orders/{ORDER_ID}")] = httpx.Response(200, json=_created_body())
    errors = []

    def worker():
        try:
            adapter.get_order(ORDER_ID)
        except Exception as e:  # pragma: no cover - surfaced by the assertion below
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert fake.token_calls == 1


def test_401_triggers_single_reauth_and_retry(adapter, fake):
    calls = []

    def handler(request):
        calls.append(request.headers["Authorization"])
        if request.headers["Authorization"] == f"Bearer {TOKEN_1}":
            return httpx.Response(401, json={"error": "invalid_token", "error_description": "Token expired"})
        return httpx.Response(201, json=_created_body())

    fake.handlers[("POST", "/v2/checkout/orders")] = handler
    order = adapter.create_order(_req())
    assert order.status == "PAYER_ACTION_REQUIRED"
    assert calls == [f"Bearer {TOKEN_1}", f"Bearer {TOKEN_2}"]
    assert fake.token_calls == 2
    # The retry is the same logical request: same idempotency key.
    assert {r.headers["PayPal-Request-Id"] for r in fake.api_requests()} == {"pact-req_abc123-create"}


def test_persistent_401_is_provider_error_after_one_retry(adapter, fake):
    fake.handlers[("GET", f"/v2/checkout/orders/{ORDER_ID}")] = httpx.Response(
        401, json={"name": "AUTHENTICATION_FAILURE", "message": "Authentication failed", "debug_id": "d1"})
    with pytest.raises(ProviderError) as exc:
        adapter.get_order(ORDER_ID)
    assert exc.value.code == "AUTHENTICATION_FAILURE"
    assert len(fake.api_requests()) == 2 and fake.token_calls == 2


@pytest.mark.parametrize("status,retryable", [(401, False), (400, False), (429, True), (500, True), (503, True)])
def test_oauth_http_failure_is_provider_error_not_timeout(adapter, fake, status, retryable):
    fake.handlers[("POST", "/v1/oauth2/token")] = httpx.Response(
        status, json={"error": "invalid_client", "error_description": "Client Authentication failed"})
    with pytest.raises(ProviderError) as exc:
        adapter.create_order(_req())
    assert exc.value.code == "PAYPAL_AUTH_FAILED"
    assert exc.value.retryable is retryable
    assert fake.api_requests() == []  # no order call without a token


@pytest.mark.parametrize("exc_type,code", [(httpx.ReadTimeout, "PAYPAL_AUTH_TIMEOUT"),
                                           (httpx.ConnectTimeout, "PAYPAL_AUTH_TIMEOUT"),
                                           (httpx.ConnectError, "PAYPAL_AUTH_UNREACHABLE")])
def test_oauth_transport_failure_is_retryable_provider_error(adapter, fake, exc_type, code):
    def boom(request):
        raise exc_type("boom", request=request)

    fake.handlers[("POST", "/v1/oauth2/token")] = boom
    with pytest.raises(ProviderError) as exc:
        adapter.capture_order(ORDER_ID, "k1")
    assert exc.value.code == code and exc.value.retryable


@pytest.mark.parametrize("resp", [httpx.Response(200, text="not json"), httpx.Response(200, json={"nope": 1}),
                                  httpx.Response(200, json={"access_token": ""})])
def test_oauth_unusable_response(adapter, fake, resp):
    fake.handlers[("POST", "/v1/oauth2/token")] = resp
    with pytest.raises(ProviderError) as exc:
        adapter.verify_credentials()
    assert exc.value.code == "PAYPAL_AUTH_INVALID_RESPONSE"


# ----------------------------------------------------------------------------- create


def test_create_order_request_shape(adapter, fake):
    fake.handlers[("POST", "/v2/checkout/orders")] = httpx.Response(201, json=_created_body())
    adapter.create_order(_req(amount_minor=4205))
    (r,) = fake.api_requests()
    assert str(r.url) == f"{SANDBOX_BASE_URL}/v2/checkout/orders"
    assert r.headers["Authorization"] == f"Bearer {TOKEN_1}"
    assert r.headers["PayPal-Request-Id"] == "pact-req_abc123-create"
    assert r.headers["Content-Type"] == "application/json"
    assert r.headers["Prefer"] == "return=representation"
    body = json.loads(r.content)
    assert body["intent"] == "CAPTURE"
    (unit,) = body["purchase_units"]
    assert unit["reference_id"] == unit["custom_id"] == unit["invoice_id"] == "req_abc123"
    assert unit["amount"] == {"currency_code": "USD", "value": "42.05"}
    assert isinstance(unit["amount"]["value"], str)
    assert unit["description"] == "Mechanical keyboard"
    ctx = body["payment_source"]["paypal"]["experience_context"]
    assert ctx["return_url"] == "http://localhost:8000/payments/return"
    assert ctx["cancel_url"] == "http://localhost:8000/payments/cancel"
    assert ctx["user_action"] == "PAY_NOW"
    assert ctx["shipping_preference"] == "NO_SHIPPING"
    assert ctx["brand_name"] == "PACT Merchant Lab"
    assert ctx["landing_page"] == "LOGIN"


@pytest.mark.parametrize("landing_page", ["LOGIN", "GUEST_CHECKOUT", "NO_PREFERENCE"])
def test_create_order_sends_configured_landing_page(fake, clock, landing_page):
    fake.handlers[("POST", "/v2/checkout/orders")] = httpx.Response(201, json=_created_body())
    a = PayPalAdapter(CLIENT_ID, CLIENT_SECRET, landing_page=landing_page, transport=httpx.MockTransport(fake),
                      clock=clock)
    try:
        a.create_order(_req())
    finally:
        a.close()
    ctx = json.loads(fake.api_requests()[0].content)["payment_source"]["paypal"]["experience_context"]
    assert ctx["landing_page"] == landing_page


@pytest.mark.parametrize("landing_page", ["login", "BILLING", "", None, "LOGIN ", ["LOGIN"]])
def test_invalid_landing_page_rejected(landing_page):
    with pytest.raises(ValueError) as exc:
        PayPalAdapter(CLIENT_ID, CLIENT_SECRET, landing_page=landing_page)
    assert "landing_page" in str(exc.value)
    assert CLIENT_SECRET not in str(exc.value)


@pytest.mark.parametrize("env,expected", [({}, "LOGIN"), ({"PAYPAL_LANDING_PAGE": "GUEST_CHECKOUT"},
                                                          "GUEST_CHECKOUT")])
def test_build_adapters_passes_landing_page_setting(monkeypatch, fake, env, expected):
    from pact_server.app import build_adapters

    for name in list(os.environ):
        if name.startswith(("PACT_", "PAYPAL_")):
            monkeypatch.delenv(name)
    settings = Settings(_env_file=None, PACT_PAYMENT_PROVIDER="paypal", PAYPAL_CLIENT_ID=CLIENT_ID,
                        PAYPAL_CLIENT_SECRET=CLIENT_SECRET, **env)
    paypal = build_adapters(settings, "http://localhost:8000")["paypal"]
    # Swap in the fake PayPal transport so the request body can be observed without any network.
    paypal._http.close()
    paypal._http = httpx.Client(base_url=SANDBOX_BASE_URL, transport=httpx.MockTransport(fake))
    fake.handlers[("POST", "/v2/checkout/orders")] = httpx.Response(201, json=_created_body())
    try:
        paypal.create_order(_req())
    finally:
        paypal.close()
    ctx = json.loads(fake.api_requests()[0].content)["payment_source"]["paypal"]["experience_context"]
    assert ctx["landing_page"] == expected


@pytest.mark.parametrize("minor,value", [(1, "0.01"), (100, "1.00"), (4200, "42.00"), (123456789, "1234567.89")])
def test_amount_sent_as_two_decimal_string(adapter, fake, minor, value):
    fake.handlers[("POST", "/v2/checkout/orders")] = httpx.Response(201, json=_created_body())
    adapter.create_order(_req(amount_minor=minor))
    assert json.loads(fake.api_requests()[0].content)["purchase_units"][0]["amount"]["value"] == value


def test_long_description_truncated_and_empty_omitted(adapter, fake):
    fake.handlers[("POST", "/v2/checkout/orders")] = httpx.Response(201, json=_created_body())
    adapter.create_order(_req(description="x" * 500))
    adapter.create_order(_req(description=""))
    first, second = (json.loads(r.content)["purchase_units"][0] for r in fake.api_requests())
    assert len(first["description"]) == 127
    assert "description" not in second


def test_create_order_parses_payer_action_url(adapter, fake):
    fake.handlers[("POST", "/v2/checkout/orders")] = httpx.Response(200, json=_created_body())
    o = adapter.create_order(_req())
    assert isinstance(o, ProviderOrder)
    assert o.provider_order_id == ORDER_ID
    assert o.status == "PAYER_ACTION_REQUIRED"
    assert o.approval_url == f"https://www.sandbox.paypal.com/checkoutnow?token={ORDER_ID}"
    assert o.capture_id is None and o.captured_amount_minor is None
    assert o.raw["reference_id"] == "req_abc123" and o.raw["provider"] == "paypal"
    assert o.raw["amount"] == {"currency_code": "USD", "value": "42.00"}


def test_payee_business_account_parsed_into_raw(adapter, fake):
    body = _created_body()
    body["purchase_units"][0]["payee"] = {"merchant_id": "MERCHANT123ABC", "email_address": "biz@example.com",
                                          "display_data": {"brand_name": "ignored"}}
    fake.handlers[("POST", "/v2/checkout/orders")] = httpx.Response(201, json=body)
    o = adapter.create_order(_req())
    assert o.raw["payee"] == {"merchant_id": "MERCHANT123ABC", "email_address": "biz@example.com"}
    _assert_no_secrets(json.dumps(o.raw))


def test_payee_parsed_on_get_order_without_payer_data(adapter, fake):
    body = _captured_body()
    body["purchase_units"][0]["payee"] = {"merchant_id": "MERCHANT123ABC"}
    fake.handlers[("GET", f"/v2/checkout/orders/{ORDER_ID}")] = httpx.Response(200, json=body)
    o = adapter.get_order(ORDER_ID)
    assert o.raw["payee"] == {"merchant_id": "MERCHANT123ABC"}
    assert "payer" not in o.raw and "buyer@example.com" not in json.dumps(o.raw)


@pytest.mark.parametrize("payee", [None, {}, "MERCHANT123ABC", ["x"], {"merchant_id": 123, "email_address": None},
                                   {"merchant_id": "", "display_data": {"brand_name": "x"}}])
def test_payee_absent_or_malformed_is_not_in_raw(adapter, fake, payee):
    body = _created_body()
    if payee is not None:
        body["purchase_units"][0]["payee"] = payee
    fake.handlers[("POST", "/v2/checkout/orders")] = httpx.Response(201, json=body)
    o = adapter.create_order(_req())
    assert "payee" not in o.raw
    assert o.provider_order_id == ORDER_ID


def test_approve_link_used_when_no_payer_action(adapter, fake):
    body = _created_body()
    body["status"] = "CREATED"
    body["links"] = [{"href": "https://www.sandbox.paypal.com/checkoutnow?token=X", "rel": "approve"},
                     {"href": "https://api.sandbox.paypal.com/v2/checkout/orders/X", "rel": "self"}]
    fake.handlers[("POST", "/v2/checkout/orders")] = httpx.Response(201, json=body)
    assert adapter.create_order(_req()).approval_url == "https://www.sandbox.paypal.com/checkoutnow?token=X"


def test_payer_action_preferred_over_approve(adapter, fake):
    body = _created_body()
    body["links"] = [{"href": "https://www.sandbox.paypal.com/payer-action", "rel": "payer-action"},
                     {"href": "https://www.sandbox.paypal.com/approve", "rel": "approve"}]
    fake.handlers[("POST", "/v2/checkout/orders")] = httpx.Response(201, json=body)
    assert adapter.create_order(_req()).approval_url.endswith("/payer-action")


@pytest.mark.parametrize("currency", ["JPY", "HUF", "TWD"])
def test_zero_decimal_currencies_rejected_before_any_call(adapter, fake, currency):
    with pytest.raises(ProviderError) as exc:
        adapter.create_order(_req(currency=currency))
    assert exc.value.code == "CURRENCY_NOT_SUPPORTED"
    assert fake.requests == []


@pytest.mark.parametrize("overrides,code", [
    ({"currency": "usd"}, "CURRENCY_NOT_SUPPORTED"),
    ({"currency": "US"}, "CURRENCY_NOT_SUPPORTED"),
    ({"amount_minor": 0}, "INVALID_AMOUNT"),
    ({"amount_minor": -5}, "INVALID_AMOUNT"),
    ({"amount_minor": 1.5}, "INVALID_AMOUNT"),
    ({"idempotency_key": ""}, "INVALID_REQUEST_ID"),
    ({"idempotency_key": "k" * 109}, "INVALID_REQUEST_ID"),
    ({"reference_id": ""}, "INVALID_REFERENCE_ID"),
    ({"reference_id": "r" * 128}, "INVALID_REFERENCE_ID"),
])
def test_invalid_create_requests_rejected_locally(adapter, fake, overrides, code):
    with pytest.raises(ProviderError) as exc:
        adapter.create_order(_req(**overrides))
    assert exc.value.code == code and not exc.value.retryable
    assert fake.requests == []


@pytest.mark.parametrize("currency", ["EUR", "GBP", "INR", "AUD"])
def test_two_decimal_currencies_accepted(adapter, fake, currency):
    fake.handlers[("POST", "/v2/checkout/orders")] = httpx.Response(201, json=_created_body())
    adapter.create_order(_req(currency=currency))
    assert json.loads(fake.api_requests()[0].content)["purchase_units"][0]["amount"]["currency_code"] == currency


# ----------------------------------------------------------------------------- capture & get


def test_capture_request_shape(adapter, fake):
    fake.handlers[("POST", f"/v2/checkout/orders/{ORDER_ID}/capture")] = httpx.Response(201, json=_captured_body())
    adapter.capture_order(ORDER_ID, "pact-req_abc123-capture")
    (r,) = fake.api_requests()
    assert str(r.url) == f"{SANDBOX_BASE_URL}/v2/checkout/orders/{ORDER_ID}/capture"
    assert r.headers["PayPal-Request-Id"] == "pact-req_abc123-capture"
    assert r.headers["Authorization"] == f"Bearer {TOKEN_1}"
    assert json.loads(r.content) == {}


@pytest.mark.parametrize("status,reason", [("COMPLETED", None), ("PENDING", "PENDING_REVIEW"),
                                           ("DECLINED", None), ("FAILED", None)])
def test_capture_parsing(adapter, fake, status, reason):
    fake.handlers[("POST", f"/v2/checkout/orders/{ORDER_ID}/capture")] = httpx.Response(
        201, json=_captured_body(status=status, value="42.05", reason=reason))
    o = adapter.capture_order(ORDER_ID, "k-cap")
    assert o.status == "COMPLETED"
    assert o.capture_id == "3C679366HH908993F"
    assert o.capture_status == status
    assert o.captured_amount_minor == 4205
    assert o.captured_currency == "USD"
    assert o.approval_url is None
    if reason:
        assert o.raw["capture_status_reason"] == reason
    # Buyer PII from the response is not copied into raw.
    assert "payer" not in o.raw and "buyer@example.com" not in json.dumps(o.raw)


def test_get_order(adapter, fake):
    fake.handlers[("GET", f"/v2/checkout/orders/{ORDER_ID}")] = httpx.Response(200, json=_captured_body())
    o = adapter.get_order(ORDER_ID)
    (r,) = fake.api_requests()
    assert r.method == "GET" and "PayPal-Request-Id" not in r.headers
    assert o.provider_order_id == ORDER_ID and o.status == "COMPLETED" and o.captured_amount_minor == 4200


def test_get_approved_order(adapter, fake):
    body = _created_body()
    body["status"] = "APPROVED"
    body["links"] = [body["links"][0]]
    fake.handlers[("GET", f"/v2/checkout/orders/{ORDER_ID}")] = httpx.Response(200, json=body)
    o = adapter.get_order(ORDER_ID)
    assert o.status == "APPROVED" and o.approval_url is None and o.capture_status is None


@pytest.mark.parametrize("bad_id", ["", "../../v1/oauth2/token", "ABC/../DEF", "a b", "X" * 65, "id?x=1"])
def test_malformed_order_ids_never_reach_the_network(adapter, fake, bad_id):
    for call in (lambda: adapter.get_order(bad_id), lambda: adapter.capture_order(bad_id, "k")):
        with pytest.raises(ProviderError) as exc:
            call()
        assert exc.value.code == "INVALID_RESOURCE_ID"
    assert fake.requests == []


def test_capture_requires_request_id(adapter, fake):
    with pytest.raises(ProviderError) as exc:
        adapter.capture_order(ORDER_ID, "")
    assert exc.value.code == "INVALID_REQUEST_ID" and fake.requests == []


# ----------------------------------------------------------------------------- error mapping


@pytest.mark.parametrize("response,code,retryable", [
    (_error(422, "UNPROCESSABLE_ENTITY", "ORDER_NOT_APPROVED"), "ORDER_NOT_APPROVED", False),
    (_error(422, "UNPROCESSABLE_ENTITY", "INSTRUMENT_DECLINED"), "INSTRUMENT_DECLINED", False),
    (_error(422, "UNPROCESSABLE_ENTITY", "ORDER_ALREADY_CAPTURED"), "ORDER_ALREADY_CAPTURED", False),
    (_error(422, "UNPROCESSABLE_ENTITY", "DUPLICATE_INVOICE_ID"), "DUPLICATE_INVOICE_ID", False),
    (_error(422, "UNPROCESSABLE_ENTITY", "PAYER_ACTION_REQUIRED"), "PAYER_ACTION_REQUIRED", False),
    (_error(400, "INVALID_REQUEST", "INVALID_PARAMETER_VALUE"), "INVALID_PARAMETER_VALUE", False),
    (_error(400, "INVALID_REQUEST"), "INVALID_REQUEST", False),
    (_error(403, "NOT_AUTHORIZED", "PERMISSION_DENIED"), "PERMISSION_DENIED", False),
    (_error(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID"), "INVALID_RESOURCE_ID", False),
    (_error(409, "RESOURCE_CONFLICT", "PREVIOUS_REQUEST_IN_PROGRESS"), "PREVIOUS_REQUEST_IN_PROGRESS", False),
    (_error(429, "RATE_LIMIT_REACHED"), "RATE_LIMIT_REACHED", True),
    (httpx.Response(429, text="slow down"), "PAYPAL_HTTP_429", True),
    (httpx.Response(418, text="<html>teapot</html>"), "PAYPAL_HTTP_418", False),
    (httpx.Response(422, json=["not", "a", "dict"]), "PAYPAL_HTTP_422", False),
])
def test_4xx_maps_to_provider_error(adapter, fake, response, code, retryable):
    fake.handlers[("POST", f"/v2/checkout/orders/{ORDER_ID}/capture")] = response
    with pytest.raises(ProviderError) as exc:
        adapter.capture_order(ORDER_ID, "k")
    assert exc.value.code == code
    assert exc.value.retryable is retryable


def test_error_message_uses_issue_description_and_debug_id(adapter, fake):
    fake.handlers[("POST", f"/v2/checkout/orders/{ORDER_ID}/capture")] = _error(
        422, "UNPROCESSABLE_ENTITY", "ORDER_NOT_APPROVED", description="Payer has not yet approved the Order")
    with pytest.raises(ProviderError) as exc:
        adapter.capture_order(ORDER_ID, "k")
    assert "Payer has not yet approved the Order" in exc.value.message
    assert "debug_id=f1e2d3c4" in exc.value.message


@pytest.mark.parametrize("status", [500, 502, 503, 504, 408])
def test_5xx_and_408_map_to_timeout(adapter, fake, status):
    fake.handlers[("POST", f"/v2/checkout/orders/{ORDER_ID}/capture")] = _error(status, "INTERNAL_SERVER_ERROR")
    with pytest.raises(ProviderTimeout):
        adapter.capture_order(ORDER_ID, "k")


@pytest.mark.parametrize("exc_type", [httpx.ReadTimeout, httpx.WriteTimeout, httpx.ConnectTimeout,
                                      httpx.PoolTimeout, httpx.ConnectError, httpx.ReadError,
                                      httpx.RemoteProtocolError])
@pytest.mark.parametrize("operation", ["create", "capture", "get"])
def test_transport_failures_map_to_timeout(adapter, fake, exc_type, operation):
    def boom(request):
        raise exc_type("connection trouble", request=request)

    for key in [("POST", "/v2/checkout/orders"), ("POST", f"/v2/checkout/orders/{ORDER_ID}/capture"),
                ("GET", f"/v2/checkout/orders/{ORDER_ID}")]:
        fake.handlers[key] = boom
    call = {"create": lambda: adapter.create_order(_req()),
            "capture": lambda: adapter.capture_order(ORDER_ID, "k"),
            "get": lambda: adapter.get_order(ORDER_ID)}[operation]
    with pytest.raises(ProviderTimeout):
        call()


@pytest.mark.parametrize("response", [
    httpx.Response(201, text="<html>proxy</html>"),
    httpx.Response(201, json=["x"]),
    httpx.Response(201),
    httpx.Response(201, json={"status": "COMPLETED"}),  # no id
    httpx.Response(201, json={"id": ORDER_ID, "status": "COMPLETED", "purchase_units": [
        {"payments": {"captures": [{"id": "C", "status": "COMPLETED", "amount": {"value": "abc"}}]}}]}),
])
def test_unreadable_success_body_is_outcome_unknown(adapter, fake, response):
    fake.handlers[("POST", f"/v2/checkout/orders/{ORDER_ID}/capture")] = response
    with pytest.raises(ProviderTimeout):
        adapter.capture_order(ORDER_ID, "k")


# ----------------------------------------------------------------------------- secret non-disclosure


def _assert_no_secrets(text: str) -> None:
    for secret in (CLIENT_SECRET, TOKEN_1, TOKEN_2):
        assert secret not in text
    b64 = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert b64 not in text


def test_repr_and_vars_hide_secrets(adapter):
    adapter.verify_credentials()
    _assert_no_secrets(repr(adapter))
    _assert_no_secrets(str(adapter))
    _assert_no_secrets(repr(vars(adapter)))
    assert "sandbox" in repr(adapter)


def test_raw_never_contains_token_or_secret(adapter, fake):
    fake.handlers[("POST", "/v2/checkout/orders")] = httpx.Response(201, json=_created_body())
    fake.handlers[("POST", f"/v2/checkout/orders/{ORDER_ID}/capture")] = httpx.Response(201, json=_captured_body())
    for o in (adapter.create_order(_req()), adapter.capture_order(ORDER_ID, "k")):
        _assert_no_secrets(repr(o))
        _assert_no_secrets(json.dumps(o.raw))


def test_error_messages_never_contain_secrets_even_if_echoed(adapter, fake):
    """A misbehaving upstream (or proxy) that echoes credentials back must not leak them into errors."""
    echo = f"bad request with {CLIENT_SECRET} and Bearer {TOKEN_1}"
    fake.handlers[("POST", f"/v2/checkout/orders/{ORDER_ID}/capture")] = httpx.Response(
        400, json={"name": "INVALID_REQUEST", "message": echo, "debug_id": "x"})
    with pytest.raises(ProviderError) as exc:
        adapter.capture_order(ORDER_ID, "k")
    _assert_no_secrets(str(exc.value))
    _assert_no_secrets(exc.value.message)
    _assert_no_secrets(repr(exc.value))


def test_oauth_failure_messages_never_contain_secrets(adapter, fake):
    fake.handlers[("POST", "/v1/oauth2/token")] = httpx.Response(
        401, json={"error": f"invalid_client {CLIENT_SECRET}", "error_description": CLIENT_SECRET})
    with pytest.raises(ProviderError) as exc:
        adapter.verify_credentials()
    _assert_no_secrets(str(exc.value))
    _assert_no_secrets(exc.value.message)


@pytest.mark.parametrize("exc_type", [httpx.ReadTimeout, httpx.ConnectError])
def test_timeout_messages_never_contain_secrets(adapter, fake, exc_type):
    def boom(request):
        raise exc_type(f"failed {request.headers['Authorization']}", request=request)

    fake.handlers[("GET", f"/v2/checkout/orders/{ORDER_ID}")] = boom
    with pytest.raises(ProviderTimeout) as exc:
        adapter.get_order(ORDER_ID)
    _assert_no_secrets(str(exc.value))

    fake.handlers[("POST", "/v1/oauth2/token")] = boom
    adapter._invalidate_token(TOKEN_1)
    with pytest.raises(ProviderError) as exc2:
        adapter.verify_credentials()
    _assert_no_secrets(str(exc2.value))


# ----------------------------------------------------------------------------- sandbox check script


def _load_check_script():
    path = Path(__file__).resolve().parents[1] / "examples" / "paypal_sandbox_check.py"
    spec = importlib.util.spec_from_file_location("paypal_sandbox_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # importing does not run main() or read settings
    return module


def test_check_script_capture_key_is_deterministic():
    mod = _load_check_script()
    assert mod.capture_check_key(ORDER_ID) == mod.capture_check_key(ORDER_ID) == f"pact-check-capture-{ORDER_ID}"
    assert mod.capture_check_key("A") != mod.capture_check_key("B")
