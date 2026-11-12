"""Simulation payment adapter.

Mirrors the PayPal Orders lifecycle without any real provider:

    create_order  -> PAYER_ACTION_REQUIRED (approval_url points at PACT's own /sim/checkout page)
    buyer_approve -> APPROVED                 buyer_cancel -> VOIDED
    capture_order -> COMPLETED (capture_status COMPLETED)

Idempotency follows ``PayPal-Request-Id`` semantics: repeating ``create_order`` with the same
idempotency key returns the same order, and repeating ``capture_order`` on the same order with the same
key replays the capture instead of failing. Results are always labelled ``provider="simulation"`` (and
``name == "simulation"``) and must never be presented as PayPal payments.

Fault injection lets tests exercise failures deterministically. A fault injected with
``after_side_effect=True`` fires *after* the order was created/captured: the caller sees a timeout although
the provider acted, which is exactly the case that requires reconciliation via ``get_order``.

Like the PayPal adapter, amounts are 2-decimal minor units, so zero-decimal currencies are rejected.
"""

from __future__ import annotations

import re
import secrets
import threading
from dataclasses import dataclass, replace

from pact_core.ports import CreatePayment, ProviderError, ProviderOrder, ProviderTimeout

OPERATIONS = ("create", "capture", "get")
FAULT_KINDS = ("timeout", "error")
_SIDE_EFFECT_OPERATIONS = ("create", "capture")
ZERO_DECIMAL_CURRENCIES = frozenset({"HUF", "JPY", "TWD"})
_CURRENCY_RE = re.compile(r"[A-Z]{3}")


@dataclass
class _SimOrder:
    id: str
    amount_minor: int
    currency: str
    status: str
    return_url: str
    cancel_url: str
    capture_id: str | None = None
    reference_id: str | None = None


@dataclass(frozen=True)
class _Fault:
    operation: str
    kind: str
    after_side_effect: bool
    code: str


class SimulationAdapter:
    name = "simulation"

    def __init__(self, public_url: str = "http://localhost:8000"):
        self.public_url = public_url.rstrip("/")
        self._orders: dict[str, _SimOrder] = {}
        self._create_idem: dict[str, str] = {}  # create idempotency key -> order id
        self._capture_idem: set[tuple[str, str]] = set()  # (order id, key) of the successful capture
        self._lock = threading.Lock()
        self._faults: list[_Fault] = []

    # ------------------------------------------------------------ fault injection (tests/demo)
    def inject(self, operation: str, kind: str, *, after_side_effect: bool = False,
               code: str = "SIMULATED_FAILURE") -> None:
        """Make the next ``operation`` (``create``/``capture``/``get``) fail with ``kind``:

        * ``"timeout"`` raises ``ProviderTimeout`` (outcome unknown);
        * ``"error"`` raises ``ProviderError(code)`` (definitive failure).

        With ``after_side_effect=True`` (create/capture only) the order is created/captured first and the
        fault fires afterwards. Faults are one-shot and consumed in injection order."""
        if operation not in OPERATIONS:
            raise ValueError(f"operation must be one of {OPERATIONS}")
        if kind not in FAULT_KINDS:
            raise ValueError(f"kind must be one of {FAULT_KINDS}")
        if after_side_effect and operation not in _SIDE_EFFECT_OPERATIONS:
            raise ValueError("after_side_effect only applies to create and capture")
        with self._lock:
            self._faults.append(_Fault(operation, kind, after_side_effect, code))

    def clear_faults(self) -> None:
        with self._lock:
            self._faults.clear()

    def _fault(self, operation: str, after: bool) -> None:
        with self._lock:
            for i, f in enumerate(self._faults):
                if f.operation == operation and f.after_side_effect == after:
                    del self._faults[i]
                    break
            else:
                return
        if f.kind == "timeout":
            when = "after" if after else "before"
            raise ProviderTimeout(f"simulated timeout in {operation} ({when} side effect); outcome unknown")
        raise ProviderError(f.code, f"simulated failure in {operation}")

    # ------------------------------------------------------------ buyer actions (checkout page)
    def buyer_approve(self, order_id: str) -> _SimOrder:
        """The simulated buyer approves the order. Idempotent; returns a snapshot."""
        with self._lock:
            o = self._get(order_id)
            if o.status == "PAYER_ACTION_REQUIRED":
                o.status = "APPROVED"
            elif o.status != "APPROVED":
                raise ProviderError("ORDER_NOT_APPROVABLE", f"Order is {o.status} and cannot be approved")
            return replace(o)

    def buyer_cancel(self, order_id: str) -> _SimOrder:
        """The simulated buyer abandons checkout: the order becomes VOIDED. Idempotent; returns a snapshot."""
        with self._lock:
            o = self._get(order_id)
            if o.status in ("PAYER_ACTION_REQUIRED", "APPROVED"):
                o.status = "VOIDED"
            elif o.status != "VOIDED":
                raise ProviderError("ORDER_ALREADY_CAPTURED", "Order already captured")
            return replace(o)

    def lookup(self, order_id: str) -> _SimOrder | None:
        """Snapshot of an order (for the simulated checkout page), or None."""
        with self._lock:
            o = self._orders.get(order_id)
            return replace(o) if o else None

    # ------------------------------------------------------------ adapter API
    def create_order(self, req: CreatePayment) -> ProviderOrder:
        if not req.idempotency_key:
            raise ProviderError("INVALID_REQUEST_ID", "An idempotency key is required")
        if not _CURRENCY_RE.fullmatch(req.currency or "") or req.currency in ZERO_DECIMAL_CURRENCIES:
            raise ProviderError("CURRENCY_NOT_SUPPORTED", "PACT amounts assume a 2-decimal ISO 4217 currency")
        if not isinstance(req.amount_minor, int) or isinstance(req.amount_minor, bool) or req.amount_minor <= 0:
            raise ProviderError("INVALID_AMOUNT", "Amount must be a positive integer number of minor units")
        self._fault("create", after=False)
        with self._lock:
            existing = self._create_idem.get(req.idempotency_key)
            if existing:
                o = self._orders[existing]
            else:
                oid = "SIM-" + secrets.token_hex(8).upper()
                o = _SimOrder(oid, req.amount_minor, req.currency, "PAYER_ACTION_REQUIRED", req.return_url,
                              req.cancel_url, reference_id=req.reference_id)
                self._orders[oid] = o
                self._create_idem[req.idempotency_key] = oid
            view = self._view(o)
        self._fault("create", after=True)
        return view

    def capture_order(self, provider_order_id: str, idempotency_key: str) -> ProviderOrder:
        if not idempotency_key:
            raise ProviderError("INVALID_REQUEST_ID", "An idempotency key is required")
        self._fault("capture", after=False)
        with self._lock:
            o = self._get(provider_order_id)
            if o.status == "COMPLETED":
                if (o.id, idempotency_key) not in self._capture_idem:
                    raise ProviderError("ORDER_ALREADY_CAPTURED", "Order already captured")
                # Same key: replay the original capture result.
            elif o.status == "VOIDED":
                raise ProviderError("ORDER_COMPLETED_OR_VOIDED", "Order is voided and cannot be captured")
            elif o.status != "APPROVED":
                raise ProviderError("ORDER_NOT_APPROVED", "Buyer has not approved the order")
            else:
                o.status = "COMPLETED"
                o.capture_id = "SIMCAP-" + secrets.token_hex(8).upper()
                self._capture_idem.add((o.id, idempotency_key))
            view = self._view(o)
        self._fault("capture", after=True)
        return view

    def get_order(self, provider_order_id: str) -> ProviderOrder:
        self._fault("get", after=False)
        with self._lock:
            return self._view(self._get(provider_order_id))

    # ------------------------------------------------------------ internals (call with the lock held)
    def _get(self, order_id: str) -> _SimOrder:
        o = self._orders.get(order_id)
        if o is None:
            raise ProviderError("RESOURCE_NOT_FOUND", "Unknown simulated order")
        return o

    def _view(self, o: _SimOrder) -> ProviderOrder:
        done = o.status == "COMPLETED"
        return ProviderOrder(
            provider_order_id=o.id,
            status=o.status,
            # Like PayPal's payer-action link, only offered while the buyer still has to act.
            approval_url=f"{self.public_url}/sim/checkout/{o.id}" if o.status == "PAYER_ACTION_REQUIRED" else None,
            capture_id=o.capture_id,
            capture_status="COMPLETED" if done else None,
            captured_amount_minor=o.amount_minor if done else None,
            captured_currency=o.currency if done else None,
            raw={"provider": "simulation", "id": o.id, "status": o.status, "reference_id": o.reference_id},
        )
