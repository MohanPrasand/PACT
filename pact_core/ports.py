"""Interfaces PACT Core depends on. Core never imports a concrete merchant or payment provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from pact_core.policy.engine import OrderFacts


class OrderSource(Protocol):
    """Read access to authoritative merchant orders plus payment-status callbacks.

    Implementations must not be called while a PACT transaction is open."""

    def get_order(self, order_id: str) -> OrderFacts | None: ...

    def merchant_exists(self, merchant_id: str) -> bool: ...

    def merchant_name(self, merchant_id: str) -> str | None: ...

    def order_summary(self, order_id: str) -> dict | None: ...

    def set_payment_status(self, order_id: str, status: str, *, authorization_id: str | None = None,
                           provider_ref: str | None = None) -> None: ...


# ---------------------------------------------------------------------------- payment providers


class ProviderError(Exception):
    """Definitive provider failure (the provider answered and said no)."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class ProviderTimeout(Exception):
    """Outcome unknown: the provider may or may not have acted. Must be reconciled, never blindly retried."""


@dataclass
class CreatePayment:
    reference_id: str  # PACT request id
    merchant_order_id: str
    amount_minor: int
    currency: str
    description: str
    return_url: str
    cancel_url: str
    idempotency_key: str


@dataclass
class ProviderOrder:
    provider_order_id: str
    status: str  # CREATED | PAYER_ACTION_REQUIRED | APPROVED | COMPLETED | VOIDED
    approval_url: str | None = None
    capture_id: str | None = None
    capture_status: str | None = None  # COMPLETED | PENDING | DECLINED | ...
    captured_amount_minor: int | None = None
    captured_currency: str | None = None
    raw: dict = field(default_factory=dict)


class PaymentAdapter(Protocol):
    name: str

    def create_order(self, req: CreatePayment) -> ProviderOrder: ...

    def capture_order(self, provider_order_id: str, idempotency_key: str) -> ProviderOrder: ...

    def get_order(self, provider_order_id: str) -> ProviderOrder: ...
