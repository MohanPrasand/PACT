"""Merchant Lab storefront backend (#24, #25): order status view and human web checkout through PACT.

Web checkout is governed exactly like an agent purchase: the browser never holds an agent key. The server
acts as the ``ag_storefront`` identity, which has its own agent policy, draws from the same shared budgets and
is subject to the same approval flow. The storefront can never approve: a REQUIRE_APPROVAL request waits for
the admin dashboard / ``pact approve``.

Everything returned here is merchant-safe: no grant token, no request hash, no policy internals beyond reason
codes, no keys or provider credentials.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any

from merchant_lab.service import MerchantLab
from pact_core.core import PactCore
from pact_core.errors import Conflict, Forbidden, PactError
from pact_core.models import State
from pact_core.seed import STOREFRONT_AGENT
from pact_core.state_machine import TERMINAL

_FAILED_ATTEMPT = {State.DENIED.value, State.FAILED.value, State.EXPIRED.value, State.REVOKED.value}
_LIVE_PROVIDER = {State.EXECUTING.value, State.UNKNOWN.value}


def safe_authorization(view: dict[str, Any]) -> dict[str, Any]:
    """Whitelist projection of a PACT request view for the public storefront."""
    decision = view.get("decision") or {}
    ex = view.get("execution") or {}
    grant = view.get("grant")
    state = view["state"]
    return {
        "request_id": view["request_id"],
        "agent_id": view["agent_id"],
        "state": state,
        "decision": decision.get("decision"),
        "reason_codes": list(decision.get("reason_codes") or []),
        "approval_pending": state == State.PENDING_APPROVAL.value,
        "decided_by": view.get("decided_by"),
        "decided_at": view.get("decided_at"),
        "grant": None if grant is None else {"issued": True, "consumed": bool(grant.get("consumed"))},
        "provider": ex.get("provider"),
        "provider_label": ex.get("provider_label"),
        "provider_order_id": ex.get("provider_order_id"),
        "capture_id": ex.get("capture_id"),
        # Only while the buyer still has to complete checkout.
        "checkout_url": ex.get("approval_url") if state == State.EXECUTING.value else None,
        "failure_reason": ex.get("failure_reason"),
        "terminal": state in TERMINAL,
        "created_at": view.get("created_at"),
        "updated_at": view.get("updated_at"),
    }


class StorefrontService:
    def __init__(self, core: PactCore, lab: MerchantLab):
        self.core, self.lab = core, lab
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

    # ------------------------------------------------------------------ read-only status
    def order_status(self, order_id: str) -> dict[str, Any]:
        order = self.lab.get_order(order_id)
        auth = None
        view = self.core.latest_request_for_order(order_id)
        if view is not None:
            if view["state"] in _LIVE_PROVIDER:
                # Same as the agent status call: pick up a buyer approval / resolve UNKNOWN. Idempotent.
                try:
                    view = self.core.reconcile(view["request_id"])
                except PactError:
                    pass
                order = self.lab.get_order(order_id)
            auth = safe_authorization(view)
        return {"order": order, "payment_status": order["payment_status"], "authorization": auth}

    # ------------------------------------------------------------------ human checkout through PACT
    def checkout(self, order_id: str, *, retry: bool = False) -> dict[str, Any]:
        """Start or resume the PACT-governed payment for an order bought on the website.

        Idempotent: while an attempt is in flight this only advances it (issues the grant after approval,
        executes, reconciles). A new attempt is created only when there is none yet, or when the previous
        one ended without payment and ``retry`` is true."""
        with self._locks[order_id]:
            return self._checkout(order_id, retry)

    def _checkout(self, order_id: str, retry: bool) -> dict[str, Any]:
        order = self.lab.get_order(order_id)
        identity = self.core.agent_identity(STOREFRONT_AGENT)
        other_agents = {a["agent_id"] for a in self.core.list_agents()} - {STOREFRONT_AGENT}
        if order["buyer_ref"] in other_agents:
            raise Forbidden("AGENT_ORDER", "This order belongs to an agent and is paid through that agent's PACT flow")
        if order["payment_status"] == "CAPTURED":
            raise Conflict("ORDER_ALREADY_PAID", "This order has already been paid")

        existing = self.core.latest_request_for_order(order_id)
        if existing is not None and existing["agent_id"] != STOREFRONT_AGENT and existing["state"] not in TERMINAL:
            raise Forbidden("AGENT_ORDER", "This order is being paid by an agent through PACT")
        if existing is not None and (existing["state"] not in _FAILED_ATTEMPT or not retry):
            return self._respond(self._advance(identity, existing))

        view = self.core.request_authorization(identity, {
            "merchant_id": order["merchant_id"], "order_id": order["order_id"],
            "amount_minor": order["total_minor"], "currency": order["currency"],
            "description": f"Web checkout at {order['merchant_name'] or order['merchant_id']}"})
        return self._respond(self._advance(identity, view))

    def _advance(self, identity, view: dict[str, Any]) -> dict[str, Any]:
        rid = view["request_id"]
        if view["state"] == State.APPROVED.value:
            view = self.core.get_status(identity, rid)  # issues the grant (token delivered once, here)
        token = view.pop("grant_token", None)
        if view["state"] == State.GRANTED.value and token:
            view = self.core.execute(identity, {
                "request_id": rid, "grant_token": token, "merchant_id": view["merchant_id"],
                "order_id": view["order_id"], "amount_minor": view["amount_minor"], "currency": view["currency"],
                "idempotency_key": f"web-{rid}"})
        elif view["state"] in _LIVE_PROVIDER:
            view = self.core.get_status(identity, rid)  # reconciles with the provider
        view.pop("grant_token", None)
        return view

    @staticmethod
    def _respond(view: dict[str, Any]) -> dict[str, Any]:
        auth = safe_authorization(view)
        return {"order_id": view["order_id"], "authorization": auth, "state": auth["state"],
                "request_id": auth["request_id"], "checkout_url": auth["checkout_url"],
                "reason_codes": auth["reason_codes"], "approval_pending": auth["approval_pending"]}
