"""PayPal Orders API v2 adapter (Sandbox).

Credentials and access tokens live only in this server-side object; nothing here is ever returned
to the browser or the agent. Every mutating call carries a ``PayPal-Request-Id`` so that a retry after
an ambiguous failure returns the original result instead of creating/capturing twice.

Docs: https://developer.paypal.com/docs/api/orders/v2/
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from pact_core.money import to_decimal_str, to_minor
from pact_core.ports import CreatePayment, ProviderError, ProviderOrder, ProviderTimeout

SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"


class PayPalAdapter:
    name = "paypal"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        base_url: str = SANDBOX_BASE_URL,
        timeout: float = 15.0,
        brand_name: str = "PACT Merchant Lab",
        transport: httpx.BaseTransport | None = None,
    ):
        if "sandbox" not in base_url and not base_url.startswith("http://"):
            # Rule 4: no real-money processing in the MVP.
            raise ValueError("PACT MVP only supports the PayPal Sandbox environment")
        self._client_id = client_id
        self._client_secret = client_secret
        self._brand = brand_name
        self._http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)
        self._token: str | None = None
        self._token_expiry = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ auth
    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._token_expiry - 60:
                return self._token
            try:
                r = self._http.post(
                    "/v1/oauth2/token",
                    data={"grant_type": "client_credentials"},
                    auth=(self._client_id, self._client_secret),
                    headers={"Accept": "application/json"},
                )
            except httpx.TimeoutException as exc:
                # No payment side-effect is possible here, so this is a plain failure.
                raise ProviderError("PAYPAL_AUTH_TIMEOUT", "Timed out obtaining PayPal access token", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise ProviderError("PAYPAL_AUTH_UNREACHABLE", str(exc), retryable=True) from exc
            if r.status_code != 200:
                raise ProviderError("PAYPAL_AUTH_FAILED", f"PayPal OAuth failed with HTTP {r.status_code}")
            body = r.json()
            self._token = body["access_token"]
            self._token_expiry = time.monotonic() + int(body.get("expires_in", 300))
            return self._token

    def _request(self, method: str, path: str, *, json: Any = None, request_id: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json",
                   "Prefer": "return=representation"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        try:
            r = self._http.request(method, path, json=json, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"PayPal {method} {path} timed out") from exc
        except httpx.TransportError as exc:
            # Connection may have dropped after PayPal processed the call: outcome unknown.
            raise ProviderTimeout(f"PayPal {method} {path} transport error: {exc}") from exc
        if r.status_code >= 500:
            raise ProviderTimeout(f"PayPal {method} {path} returned HTTP {r.status_code}")
        if r.status_code >= 400:
            code, message = _error_details(r)
            raise ProviderError(code, message)
        return r.json() if r.content else {}

    # ------------------------------------------------------------------ orders
    def create_order(self, req: CreatePayment) -> ProviderOrder:
        body = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": req.reference_id,
                    "custom_id": req.reference_id,
                    "invoice_id": req.reference_id,
                    "description": req.description[:127],
                    "amount": {"currency_code": req.currency, "value": to_decimal_str(req.amount_minor)},
                }
            ],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "brand_name": self._brand,
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
        data = self._request("POST", f"/v2/checkout/orders/{provider_order_id}/capture", json={}, request_id=idempotency_key)
        return _parse_order(data)

    def get_order(self, provider_order_id: str) -> ProviderOrder:
        return _parse_order(self._request("GET", f"/v2/checkout/orders/{provider_order_id}"))


def _error_details(r: httpx.Response) -> tuple[str, str]:
    try:
        body = r.json()
    except ValueError:
        return f"PAYPAL_HTTP_{r.status_code}", r.text[:200]
    issue = None
    details = body.get("details") or []
    if details and isinstance(details, list):
        issue = details[0].get("issue")
    code = issue or body.get("name") or f"PAYPAL_HTTP_{r.status_code}"
    msg = (details[0].get("description") if details else None) or body.get("message") or code
    return code, msg


def _parse_order(data: dict) -> ProviderOrder:
    approval_url = None
    for link in data.get("links", []):
        if link.get("rel") in ("payer-action", "approve"):
            approval_url = link.get("href")
    capture_id = capture_status = captured_currency = None
    captured_amount = None
    for unit in data.get("purchase_units", []) or []:
        captures = ((unit.get("payments") or {}).get("captures")) or []
        if captures:
            c = captures[0]
            capture_id = c.get("id")
            capture_status = c.get("status")
            amt = c.get("amount") or {}
            if amt.get("value") is not None:
                captured_amount = to_minor(amt["value"])
                captured_currency = amt.get("currency_code")
    return ProviderOrder(
        provider_order_id=data.get("id", ""),
        status=data.get("status", "UNKNOWN"),
        approval_url=approval_url,
        capture_id=capture_id,
        capture_status=capture_status,
        captured_amount_minor=captured_amount,
        captured_currency=captured_currency,
        raw={"id": data.get("id"), "status": data.get("status")},
    )
