"""Simulation payment adapter.

Mirrors the PayPal Orders lifecycle (create -> buyer approval -> capture) without any real provider.
Results are always labelled ``provider="simulation"`` and must never be presented as PayPal payments.
Fault injection hooks exist so tests can exercise timeouts and failures deterministically.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass

from pact_core.ports import CreatePayment, ProviderError, ProviderOrder, ProviderTimeout


@dataclass
class _SimOrder:
    id: str
    amount_minor: int
    currency: str
    status: str
    return_url: str
    cancel_url: str
    capture_id: str | None = None


class SimulationAdapter:
    name = "simulation"

    def __init__(self, public_url: str = "http://localhost:8000"):
        self.public_url = public_url.rstrip("/")
        self._orders: dict[str, _SimOrder] = {}
        self._idem: dict[str, str] = {}  # idempotency key -> order id
        self._lock = threading.Lock()
        # Fault injection: list of (operation, kind, apply_after) — kind in {"timeout", "error"}.
        self._faults: list[tuple[str, str, bool]] = []

    # ------------------------------------------------------------ fault injection (tests/demo)
    def inject(self, operation: str, kind: str, *, after_side_effect: bool = False) -> None:
        """Make the next ``operation`` fail. ``after_side_effect`` simulates a timeout *after* the
        provider already acted (the dangerous case that requires reconciliation)."""
        self._faults.append((operation, kind, after_side_effect))

    def _fault(self, operation: str, after: bool) -> None:
        for i, (op, kind, aft) in enumerate(self._faults):
            if op == operation and aft == after:
                self._faults.pop(i)
                if kind == "timeout":
                    raise ProviderTimeout(f"simulated timeout in {operation}")
                raise ProviderError("SIMULATED_FAILURE", f"simulated failure in {operation}")

    # ------------------------------------------------------------ buyer actions (checkout page)
    def buyer_approve(self, order_id: str) -> _SimOrder:
        with self._lock:
            o = self._orders[order_id]
            if o.status == "PAYER_ACTION_REQUIRED":
                o.status = "APPROVED"
            return o

    def lookup(self, order_id: str) -> _SimOrder | None:
        return self._orders.get(order_id)

    # ------------------------------------------------------------ adapter API
    def create_order(self, req: CreatePayment) -> ProviderOrder:
        self._fault("create", after=False)
        with self._lock:
            existing = self._idem.get(req.idempotency_key)
            if existing:
                o = self._orders[existing]
            else:
                oid = "SIM-" + secrets.token_hex(6).upper()
                o = _SimOrder(oid, req.amount_minor, req.currency, "PAYER_ACTION_REQUIRED", req.return_url, req.cancel_url)
                self._orders[oid] = o
                self._idem[req.idempotency_key] = oid
        self._fault("create", after=True)
        return self._view(o)

    def capture_order(self, provider_order_id: str, idempotency_key: str) -> ProviderOrder:
        self._fault("capture", after=False)
        with self._lock:
            o = self._orders.get(provider_order_id)
            if o is None:
                raise ProviderError("RESOURCE_NOT_FOUND", "Unknown simulated order")
            if o.status == "COMPLETED":
                if self._idem.get(idempotency_key) != o.id:
                    raise ProviderError("ORDER_ALREADY_CAPTURED", "Order already captured")
            elif o.status != "APPROVED":
                raise ProviderError("ORDER_NOT_APPROVED", "Buyer has not approved the order")
            else:
                o.status = "COMPLETED"
                o.capture_id = "SIMCAP-" + secrets.token_hex(6).upper()
                self._idem[idempotency_key] = o.id
        self._fault("capture", after=True)
        return self._view(o)

    def get_order(self, provider_order_id: str) -> ProviderOrder:
        self._fault("get", after=False)
        o = self._orders.get(provider_order_id)
        if o is None:
            raise ProviderError("RESOURCE_NOT_FOUND", "Unknown simulated order")
        return self._view(o)

    def _view(self, o: _SimOrder) -> ProviderOrder:
        done = o.status == "COMPLETED"
        return ProviderOrder(
            provider_order_id=o.id,
            status=o.status,
            approval_url=f"{self.public_url}/sim/checkout/{o.id}",
            capture_id=o.capture_id,
            capture_status="COMPLETED" if done else None,
            captured_amount_minor=o.amount_minor if done else None,
            captured_currency=o.currency if done else None,
        )
