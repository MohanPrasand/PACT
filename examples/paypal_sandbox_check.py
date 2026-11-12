"""Verify PayPal Sandbox credentials and currency eligibility without exposing secrets.

    uv run python examples/paypal_sandbox_check.py [--currency USD] [--capture ORDER_ID]

1. Obtains an OAuth access token (client credentials).
2. Creates a small CAPTURE order and prints the buyer approval URL.
3. With --capture ORDER_ID: captures an order you approved in the browser with a sandbox
   personal account, and prints the capture result. The capture uses the deterministic
   idempotency key ``pact-check-capture-<ORDER_ID>``, so re-running it replays the same capture
   instead of capturing twice.

Credentials are read from .env; neither they nor the access token are printed. This script talks to
the real PayPal Sandbox, so it is run manually only (never from the test suite).
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.paypal.adapter import PayPalAdapter  # noqa: E402
from pact_core.config import get_settings  # noqa: E402
from pact_core.money import to_decimal_str  # noqa: E402
from pact_core.ports import CreatePayment, ProviderError, ProviderTimeout  # noqa: E402


def capture_check_key(order_id: str) -> str:
    """Deterministic PayPal-Request-Id for ``--capture``: re-running replays rather than re-captures."""
    return f"pact-check-capture-{order_id}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--currency", default=None)
    ap.add_argument("--amount-minor", type=int, default=100)
    ap.add_argument("--capture", metavar="ORDER_ID")
    args = ap.parse_args()

    s = get_settings()
    if not s.paypal_client_id or not s.paypal_client_secret:
        print("FAIL: PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not set in .env")
        return 1
    if args.amount_minor <= 0:
        print("FAIL: --amount-minor must be a positive integer")
        return 1
    try:
        adapter = PayPalAdapter(
            s.paypal_client_id, s.paypal_client_secret,
            base_url=s.paypal_base_url, timeout=s.paypal_timeout_seconds,
        )
    except ValueError as e:
        print(f"FAIL: {e}")
        return 1
    currency = (args.currency or s.currency).upper()
    # Only scheme and host: never echo anything else from configuration.
    parts = urlsplit(s.paypal_base_url)
    print(f"Environment: {parts.scheme}://{parts.hostname}")

    try:
        adapter.verify_credentials()
        print("OK   OAuth access token obtained")

        if args.capture:
            o = adapter.capture_order(args.capture, capture_check_key(args.capture))
            print(f"OK   Capture: order={o.provider_order_id} status={o.status} capture_id={o.capture_id} "
                  f"capture_status={o.capture_status} amount={o.captured_amount_minor} {o.captured_currency}")
            return 0

        ref = "chk_" + secrets.token_hex(6)
        o = adapter.create_order(CreatePayment(
            reference_id=ref, merchant_order_id=ref, amount_minor=args.amount_minor, currency=currency,
            description="PACT sandbox connectivity check", return_url=f"{s.public_url}/payments/return",
            cancel_url=f"{s.public_url}/payments/cancel", idempotency_key=f"pact-{ref}-create",
        ))
        print(f"OK   Order created in {currency} {to_decimal_str(args.amount_minor)}: id={o.provider_order_id} status={o.status}")
        print(f"     Buyer approval URL: {o.approval_url}")
        again = adapter.get_order(o.provider_order_id)
        print(f"OK   Order retrieval: status={again.status}")
    except ProviderError as e:
        print(f"FAIL {e.code}: {e.message}")
        return 1
    except ProviderTimeout as e:
        print(f"FAIL outcome unknown (timeout/5xx): {e}")
        return 1
    finally:
        adapter.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
