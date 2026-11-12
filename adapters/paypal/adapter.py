"""PayPal Orders API v2 adapter (Sandbox only).

Credentials and access tokens live only in this server-side object: they are never returned, logged,
put in exception messages or copied into ``ProviderOrder.raw``. Every mutating call carries a
``PayPal-Request-Id`` so a retry after an ambiguous failure returns the original result instead of
creating or capturing twice.

Error semantics (what the execution gateway relies on):

* ``ProviderError``: PayPal answered and the call definitely had no payment side effect (4xx, an OAuth
  failure, or a request rejected locally before anything was sent). ``retryable`` is True for rate
  limits (429) and transient OAuth/auth-server problems.
* ``ProviderTimeout``: the outcome is unknown (timeout, dropped connection, 5xx, 408, or an unreadable
  2xx body). PayPal may have acted, so the caller must reconcile with ``get_order``, never blindly retry.

Amounts: PACT money is integer minor units with two decimal places (``pact_core.money``). Currencies
that PayPal requires to be sent without decimals (HUF, JPY, TWD) are rejected up front instead of being
silently mis-scaled.

Docs: https://developer.paypal.com/docs/api/orders/v2/
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from pact_core.money import to_decimal_str, to_minor
from pact_core.ports import CreatePayment, ProviderError, ProviderOrder, ProviderTimeout

SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"

# Mirrors pact_core.config: hosts that can only ever reach a local mock/test server, never real PayPal.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "testserver"})
_LOCAL_SUFFIXES = (".localhost", ".test")

# PayPal expects these currencies without a fractional part; PACT's minor units assume 2 decimals.
ZERO_DECIMAL_CURRENCIES = frozenset({"HUF", "JPY", "TWD"})
_CURRENCY_RE = re.compile(r"[A-Z]{3}")
# PayPal order ids are short uppercase alphanumerics; anything else must never reach a URL path.
_ORDER_ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
# Conservative PayPal-Request-Id bound (the historic documented limit; the current schema allows more).
MAX_REQUEST_ID_LENGTH = 108
# Refresh the token this long before PayPal says it expires.
TOKEN_REFRESH_MARGIN_SECONDS = 60
_MAX_MESSAGE = 300


def is_sandbox_base_url(url: str) -> bool:
    """True only for an https PayPal Sandbox host or a local test server.

    Same rule as ``Settings.paypal_base_url`` in ``pact_core.config``: the host must have a
    ``sandbox`` label over https, or be localhost/127.0.0.1/::1/testserver/*.localhost/*.test.
    URLs with embedded credentials are refused outright."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    if parts.scheme in ("http", "https") and host and (host in _LOCAL_HOSTS or host.endswith(_LOCAL_SUFFIXES)):
        return True
    return parts.scheme == "https" and "sandbox" in host.split(".")


class _Secret:
    """Holds a credential so that ``repr``/``vars``/tracebacks never show its value."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def get(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "_Secret('**********')"

    __str__ = __repr__


class PayPalAdapter:
    name = "paypal"

    def __init__(
        self,
        client_id: str,
        client_secret: Any,
        *,
        base_url: str = SANDBOX_BASE_URL,
        timeout: float = 15.0,
        brand_name: str = "PACT Merchant Lab",
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        """``client_secret`` may be a ``str`` or a pydantic ``SecretStr``. ``clock`` is a monotonic
        seconds source, injectable so tests can drive token expiry."""
        # Rule 4: no real-money processing. The URL is not echoed in the error (it may embed credentials).
        if not is_sandbox_base_url(base_url):
            raise ValueError(
                "PACT only supports the PayPal Sandbox (e.g. https://api-m.sandbox.paypal.com) or a local "
                "test server; live PayPal endpoints are not allowed"
            )
        if hasattr(client_secret, "get_secret_value"):
            client_secret = client_secret.get_secret_value()
        if not client_id or not client_secret:
            raise ValueError("PayPal client id and client secret are required")
        self._client_id = client_id
        self._client_secret = _Secret(client_secret)
        self._brand = brand_name
        self._base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=self._base_url, timeout=timeout, transport=transport)
        self._clock = clock
        self._token: _Secret | None = None
        self._token_refresh_at = 0.0
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"PayPalAdapter(base_url={self._base_url!r})"

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ auth
    def verify_credentials(self) -> None:
        """Obtain (or reuse) an access token; raises ``ProviderError`` if the credentials are rejected."""
        self._access_token()

    def _access_token(self) -> str:
        # The lock is held across the token request so concurrent callers share one fetch.
        with self._lock:
            if self._token is not None and self._clock() < self._token_refresh_at:
                return self._token.get()
            try:
                r = self._http.post(
                    "/v1/oauth2/token",
                    data={"grant_type": "client_credentials"},
                    auth=(self._client_id, self._client_secret.get()),
                    headers={"Accept": "application/json"},
                )
            except httpx.TimeoutException as exc:
                # No payment side effect is possible here, so this is a plain (retryable) failure.
                raise ProviderError("PAYPAL_AUTH_TIMEOUT", "Timed out obtaining a PayPal access token",
                                    retryable=True) from exc
            except httpx.HTTPError as exc:
                raise ProviderError("PAYPAL_AUTH_UNREACHABLE",
                                    f"Could not reach PayPal OAuth endpoint ({type(exc).__name__})",
                                    retryable=True) from exc
            if r.status_code != 200:
                code = "PAYPAL_AUTH_FAILED"
                detail = ""
                try:
                    body = r.json()
                    if isinstance(body, dict) and isinstance(body.get("error"), str):
                        detail = f" ({body['error']})"
                except ValueError:
                    pass
                retryable = r.status_code == 429 or r.status_code >= 500
                raise ProviderError(code, self._redact(f"PayPal OAuth failed with HTTP {r.status_code}{detail}"),
                                    retryable=retryable)
            try:
                body = r.json()
                token = body["access_token"]
                expires_in = int(body.get("expires_in", 300))
                if not isinstance(token, str) or not token:
                    raise TypeError
            except (ValueError, KeyError, TypeError) as exc:
                raise ProviderError("PAYPAL_AUTH_INVALID_RESPONSE", "PayPal OAuth returned an unusable response",
                                    retryable=True) from exc
            # Refresh ``expires_in - 60s`` from now (or halfway, for unusually short-lived tokens).
            margin = min(TOKEN_REFRESH_MARGIN_SECONDS, expires_in / 2)
            self._token = _Secret(token)
            self._token_refresh_at = self._clock() + expires_in - margin
            return token

    def _invalidate_token(self, token: str) -> None:
        with self._lock:
            # Only drop the token that was rejected; another thread may already have refreshed it.
            if self._token is not None and self._token.get() == token:
                self._token = None
                self._token_refresh_at = 0.0

    def _redact(self, text: str) -> str:
        for secret in (self._client_secret, self._token):
            if secret is not None and secret.get():
                text = text.replace(secret.get(), "**********")
        return text[:_MAX_MESSAGE]

    # ------------------------------------------------------------------ transport
    def _request(self, method: str, path: str, *, json: Any = None, request_id: str | None = None) -> dict:
        op = f"PayPal {method} {path}"
        for attempt in (1, 2):
            token = self._access_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Prefer": "return=representation",
            }
            if json is not None:
                headers["Content-Type"] = "application/json"
            if request_id:
                headers["PayPal-Request-Id"] = request_id
            try:
                r = self._http.request(method, path, json=json, headers=headers)
            except httpx.TimeoutException as exc:
                raise ProviderTimeout(f"{op} timed out; outcome unknown") from exc
            except httpx.TransportError as exc:
                # The connection may have dropped after PayPal processed the call: outcome unknown.
                raise ProviderTimeout(f"{op} transport error ({type(exc).__name__}); outcome unknown") from exc
            if r.status_code == 401 and attempt == 1:
                # Expired/revoked token: PayPal rejected the call before acting. Re-authenticate once.
                self._invalidate_token(token)
                continue
            break
        if r.status_code >= 500 or r.status_code == 408:
            raise ProviderTimeout(f"{op} returned HTTP {r.status_code}; outcome unknown")
        if r.status_code >= 400:
            code, message = _error_details(r)
            raise ProviderError(code, self._redact(message), retryable=r.status_code == 429)
        if not r.content:
            return {}
        try:
            data = r.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            # PayPal accepted the call but we cannot tell what happened.
            raise ProviderTimeout(f"{op} returned an unreadable HTTP {r.status_code} body; outcome unknown")
        return data

    # ------------------------------------------------------------------ orders
    def create_order(self, req: CreatePayment) -> ProviderOrder:
        _check_request_id(req.idempotency_key)
        currency = _check_currency(req.currency)
        if not isinstance(req.amount_minor, int) or isinstance(req.amount_minor, bool) or req.amount_minor <= 0:
            raise ProviderError("INVALID_AMOUNT", "Amount must be a positive integer number of minor units")
        if not req.reference_id or len(req.reference_id) > 127:
            # invoice_id is the tightest PayPal limit (127 chars).
            raise ProviderError("INVALID_REFERENCE_ID", "reference_id must be 1-127 characters")
        unit: dict[str, Any] = {
            "reference_id": req.reference_id,
            "custom_id": req.reference_id,
            "invoice_id": req.reference_id,
            "amount": {"currency_code": currency, "value": to_decimal_str(req.amount_minor)},
        }
        if req.description:
            unit["description"] = req.description[:127]
        body = {
            "intent": "CAPTURE",
            "purchase_units": [unit],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "brand_name": self._brand[:127],
                        "shipping_preference": "NO_SHIPPING",
                        "user_action": "PAY_NOW",
                        "return_url": req.return_url,
                        "cancel_url": req.cancel_url,
                    }
                }
            },
        }
        return _parse_order(self._request("POST", "/v2/checkout/orders", json=body, request_id=req.idempotency_key))

    def capture_order(self, provider_order_id: str, idempotency_key: str) -> ProviderOrder:
        _check_order_id(provider_order_id)
        _check_request_id(idempotency_key)
        data = self._request("POST", f"/v2/checkout/orders/{provider_order_id}/capture", json={},
                             request_id=idempotency_key)
        return _parse_order(data)

    def get_order(self, provider_order_id: str) -> ProviderOrder:
        _check_order_id(provider_order_id)
        return _parse_order(self._request("GET", f"/v2/checkout/orders/{provider_order_id}"))


# ---------------------------------------------------------------------- helpers


def _check_order_id(order_id: str) -> None:
    if not isinstance(order_id, str) or not _ORDER_ID_RE.fullmatch(order_id):
        raise ProviderError("INVALID_RESOURCE_ID", "Malformed PayPal order id")


def _check_request_id(request_id: str) -> None:
    if not isinstance(request_id, str) or not request_id or len(request_id) > MAX_REQUEST_ID_LENGTH:
        raise ProviderError("INVALID_REQUEST_ID",
                            f"PayPal-Request-Id must be 1-{MAX_REQUEST_ID_LENGTH} characters")


def _check_currency(currency: str) -> str:
    if not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency):
        raise ProviderError("CURRENCY_NOT_SUPPORTED", "Currency must be a 3-letter uppercase ISO 4217 code")
    if currency in ZERO_DECIMAL_CURRENCIES:
        raise ProviderError("CURRENCY_NOT_SUPPORTED",
                            f"{currency} has no minor unit at PayPal; PACT amounts assume 2 decimal places")
    return currency


def _error_details(r: httpx.Response) -> tuple[str, str]:
    """Map a PayPal 4xx body to ``(code, message)``: ``details[0].issue``, else ``name``/``error``."""
    fallback = f"PAYPAL_HTTP_{r.status_code}"
    try:
        body = r.json()
    except ValueError:
        return fallback, f"PayPal returned HTTP {r.status_code}"
    if not isinstance(body, dict):
        return fallback, f"PayPal returned HTTP {r.status_code}"
    details = body.get("details")
    first = details[0] if isinstance(details, list) and details and isinstance(details[0], dict) else {}
    issue = first.get("issue") if isinstance(first.get("issue"), str) else None
    name = body.get("name") if isinstance(body.get("name"), str) else None
    oauth_error = body.get("error") if isinstance(body.get("error"), str) else None
    code = issue or name or oauth_error or fallback
    message = first.get("description") or body.get("message") or body.get("error_description") or code
    if not isinstance(message, str):
        message = code
    debug_id = body.get("debug_id")
    if isinstance(debug_id, str) and debug_id:
        message = f"{message} (debug_id={debug_id})"
    return code, message


def _parse_order(data: dict) -> ProviderOrder:
    """Parse an order body. A 2xx we cannot interpret means PayPal may have acted: outcome unknown."""
    try:
        order = _parse_order_body(data)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ProviderTimeout("PayPal returned an order body PACT could not parse; outcome unknown") from exc
    if not order.provider_order_id or order.status == "UNKNOWN":
        raise ProviderTimeout("PayPal response is missing the order id or status; outcome unknown")
    return order


def _parse_order_body(data: dict) -> ProviderOrder:
    links = data.get("links") or []
    by_rel = {link.get("rel"): link.get("href") for link in links if isinstance(link, dict)}
    approval_url = by_rel.get("payer-action") or by_rel.get("approve")

    capture_id = capture_status = captured_currency = capture_reason = None
    captured_amount = None
    unit0: dict = {}
    units = data.get("purchase_units") or []
    if units and isinstance(units[0], dict):
        unit0 = units[0]
    for unit in units:
        captures = ((unit.get("payments") or {}).get("captures")) or []
        if captures:
            c = captures[0]
            capture_id = c.get("id")
            capture_status = c.get("status")
            capture_reason = (c.get("status_details") or {}).get("reason")
            amt = c.get("amount") or {}
            if amt.get("value") is not None:
                captured_amount = to_minor(amt["value"])
                captured_currency = amt.get("currency_code")
            break

    # Only non-sensitive identifiers the gateway may use to verify the order belongs to the request.
    raw: dict[str, Any] = {"provider": "paypal", "id": data.get("id"), "status": data.get("status")}
    for key in ("reference_id", "custom_id", "invoice_id"):
        if unit0.get(key) is not None:
            raw[key] = unit0[key]
    amount = unit0.get("amount") or {}
    if amount.get("value") is not None:
        raw["amount"] = {"currency_code": amount.get("currency_code"), "value": amount.get("value")}
    if capture_reason:
        raw["capture_status_reason"] = capture_reason

    return ProviderOrder(
        provider_order_id=data.get("id") or "",
        status=data.get("status") or "UNKNOWN",
        approval_url=approval_url,
        capture_id=capture_id,
        capture_status=capture_status,
        captured_amount_minor=captured_amount,
        captured_currency=captured_currency,
        raw=raw,
    )
