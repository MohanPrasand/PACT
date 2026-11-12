"""Hashing and identifier helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Any

GENESIS_HASH = "0" * 64


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def new_secret(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def request_hash(
    *,
    agent_id: str,
    user_id: str,
    merchant_id: str,
    order_id: str,
    action_type: str,
    amount_minor: int,
    currency: str,
) -> str:
    """Binding hash over every field that defines *what* is being paid. Any change => new hash."""
    return sha256_hex(
        canonical_json(
            {
                "agent_id": agent_id,
                "user_id": user_id,
                "merchant_id": merchant_id,
                "order_id": order_id,
                "action_type": action_type,
                "amount_minor": int(amount_minor),
                "currency": currency.upper(),
            }
        )
    )
