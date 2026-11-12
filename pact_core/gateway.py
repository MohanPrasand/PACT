"""Execution gateway (#10): the only code path that calls a payment adapter.

execute():  validate grant + bindings + live order + reservation  →  atomically consume the grant and
            move GRANTED → EXECUTING (one transaction)  →  create the provider order (outside it).
Buyer approves at the provider  →  handle_provider_return() captures  →  verify  →  EXECUTED + settle.

Ambiguous provider outcomes (timeout, 5xx, dropped connection) move the request to UNKNOWN. It is left
only through reconcile(), which asks the provider what happened. Payments are never blindly retried;
provider calls carry deterministic idempotency keys (``pact-<request>-create|capture``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import select

from pact_core.crypto import constant_time_equal, request_hash, sha256_hex
from pact_core.errors import Conflict, NotFound, PactError
from pact_core.grants import GrantClaim
from pact_core.models import AuthorizationRequest, State
from pact_core.ports import CreatePayment, ProviderError, ProviderOrder, ProviderTimeout
from pact_core.runtime import AgentIdentity, PactContext, request_view
from pact_core.state_machine import transition

LIVE = (State.EXECUTING.value, State.UNKNOWN.value)
NOT_READY = {"ORDER_NOT_APPROVED", "PAYER_ACTION_REQUIRED"}
BUYER_RETRY = {"INSTRUMENT_DECLINED"}
# "No such order" from the provider: the simulation adapter's code and PayPal's 404 issue (#38).
NOT_FOUND = {"RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID"}
CAPTURE_OUTCOME_UNKNOWN = "PROVIDER_TIMEOUT_ON_CAPTURE"


class ExecuteInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: str = Field(min_length=1, max_length=40)
    grant_token: str = Field(min_length=1, max_length=200)
    merchant_id: str = Field(min_length=1, max_length=64)
    order_id: str = Field(min_length=1, max_length=64)
    amount_minor: StrictInt
    currency: str = Field(min_length=3, max_length=3)
    idempotency_key: str | None = Field(None, min_length=8, max_length=128)


class ExecutionGateway:
    def __init__(self, ctx: PactContext):
        self.ctx = ctx

    # ------------------------------------------------------------------ execute
    def execute(self, agent: AgentIdentity, inp: ExecuteInput) -> dict[str, Any]:
        ctx = self.ctx
        actor = f"agent:{agent.id}"
        order = ctx.orders.get_order(inp.order_id)  # authoritative, read before the transaction

        rejection: PactError | None = None
        with ctx.db.transaction() as s:
            req = ctx.load_request(s, inp.request_id, agent=agent)  # 1-2: caller owns the request
            grant = ctx.grant_for(s, req.id)

            # Safe retry: same token + same idempotency key returns the existing execution.
            if (inp.idempotency_key and req.execute_idempotency_key == inp.idempotency_key and grant is not None
                    and constant_time_equal(sha256_hex(inp.grant_token), grant.token_hash)):
                return request_view(req, grant)

            live_hash = (request_hash(agent_id=req.agent_id, user_id=req.user_id, merchant_id=order.merchant_id,
                                      order_id=order.order_id, action_type=req.action_type,
                                      amount_minor=order.amount_minor, currency=order.currency)
                         if order is not None else "ORDER_NOT_FOUND")
            claim = GrantClaim(request_id=req.id, token=inp.grant_token, agent_id=agent.id,
                               merchant_id=inp.merchant_id, order_id=inp.order_id,
                               amount_minor=inp.amount_minor, currency=inp.currency)
            try:
                # 3-8: grant valid, unused, unexpired, bound to this merchant/order/amount/currency,
                # request hash intact, and the live merchant order unchanged since authorization.
                grant = ctx.grants.validate(s, claim, current_request_hash=live_hash)
                if req.state != State.GRANTED.value:
                    raise Conflict("INVALID_STATE", f"Request is {req.state}, not GRANTED")
                if not ctx.budget.active_reservations(s, req.id):
                    raise Conflict("RESERVATION_MISSING", "No active budget reservation backs this grant")
                ctx.grants.consume(s, grant)  # atomic single-use
            except PactError as exc:
                rejection = exc
                if exc.code == "GRANT_EXPIRED" and req.state == State.GRANTED.value:
                    transition(s, req, State.EXPIRED, ctx.clock)
                    req.failure_reason = "GRANT_EXPIRED"
                    ctx.audit(s, "GRANT_EXPIRED", "system", req.id,
                              reservations_released=ctx.budget.release(s, req.id))
                event = "REPLAY_REJECTED" if exc.code == "GRANT_ALREADY_USED" else "EXECUTION_REJECTED"
                ctx.audit(s, event, actor, req.id, code=exc.code, message=exc.message, details=exc.details,
                          claimed={"merchant_id": inp.merchant_id, "order_id": inp.order_id,
                                   "amount_minor": inp.amount_minor, "currency": inp.currency})
            else:
                # 9-10: idempotency + transition into execution, in the same transaction as the consume.
                transition(s, req, State.EXECUTING, ctx.clock)
                req.provider = ctx.provider
                req.execute_idempotency_key = inp.idempotency_key
                ctx.audit(s, "GRANT_CONSUMED", actor, req.id, grant_id=grant.id)
                ctx.audit(s, "EXECUTION_STARTED", actor, req.id, provider=ctx.provider)
        if rejection is not None:
            raise rejection

        try:
            ctx.orders.set_payment_status(req.order_id, "PENDING", authorization_id=req.id)
        except PactError as exc:
            # Nothing has been sent to the provider yet, so failing here moves no money.
            return self._fail(req.id, f"MERCHANT_REJECTED:{exc.code}", merchant_status=None)
        return self._create_provider_order(req.id)

    # ------------------------------------------------------------------ provider steps
    def _create_provider_order(self, request_id: str) -> dict[str, Any]:
        ctx = self.ctx
        with ctx.db.transaction() as s:
            req = ctx.load_request(s, request_id)
            if req.state not in LIVE:
                return request_view(req, ctx.grant_for(s, req.id))
            adapter = ctx.adapters[req.provider or ctx.provider]
            payment = CreatePayment(
                reference_id=req.id, merchant_order_id=req.order_id, amount_minor=req.amount_minor,
                currency=req.currency, description=(req.description or f"PACT order {req.order_id}")[:127],
                return_url=f"{ctx.public_url}/payments/return", cancel_url=f"{ctx.public_url}/payments/cancel",
                idempotency_key=f"pact-{req.id}-create",
            )
        try:
            po = adapter.create_order(payment)
        except ProviderTimeout as exc:
            return self._unknown(request_id, "PROVIDER_TIMEOUT_ON_CREATE", str(exc))
        except ProviderError as exc:
            return self._fail(request_id, f"PROVIDER_REJECTED:{exc.code}")

        with ctx.db.transaction() as s:
            req = ctx.load_request(s, request_id)
            if req.state == State.UNKNOWN.value:
                transition(s, req, State.EXECUTING, ctx.clock)
            req.provider_order_id = po.provider_order_id
            req.provider_status = po.status
            req.approval_url = po.approval_url
            ctx.audit(s, "PROVIDER_ORDER_CREATED", "gateway", req.id, provider=req.provider,
                      provider_order_id=po.provider_order_id, status=po.status)
            order_id = req.order_id
        self._merchant(order_id, "PENDING", authorization_id=request_id, provider_ref=po.provider_order_id)
        return self._view(request_id)

    def _capture(self, request_id: str) -> dict[str, Any]:
        ctx = self.ctx
        with ctx.db.transaction() as s:
            req = ctx.load_request(s, request_id)
            if req.state not in LIVE or not req.provider_order_id:
                return request_view(req, ctx.grant_for(s, req.id))
            adapter, poid = ctx.adapters[req.provider], req.provider_order_id
        try:
            po = adapter.capture_order(poid, f"pact-{request_id}-capture")
        except ProviderTimeout as exc:
            return self._unknown(request_id, "PROVIDER_TIMEOUT_ON_CAPTURE", str(exc))
        except ProviderError as exc:
            if exc.code in NOT_READY | BUYER_RETRY:
                self._note_status(request_id, exc.code)
                return self._view(request_id)
            return self._after_capture_rejected(request_id, adapter, poid, exc)
        return self._finalize(request_id, po)

    def _after_capture_rejected(self, request_id: str, adapter, poid: str, exc: ProviderError) -> dict[str, Any]:
        """The provider refused this capture call. Look at the order once and decide; never capture again
        from here (#39: capture -> reconcile -> capture recursed without bound)."""
        try:
            po = adapter.get_order(poid)
        except (ProviderTimeout, ProviderError):
            self._note_status(request_id, exc.code)  # cannot tell yet; the next poll retries
            return self._view(request_id)
        if po.status == "COMPLETED" or po.capture_id or po.capture_status:
            return self._finalize(request_id, po)  # e.g. ORDER_ALREADY_CAPTURED: money moved earlier
        if exc.retryable:
            self._note_status(request_id, exc.code)  # rate limit / transient auth: the next poll retries
            return self._view(request_id)
        return self._fail(request_id, f"CAPTURE_REJECTED:{exc.code}")

    def _finalize(self, request_id: str, po: ProviderOrder) -> dict[str, Any]:
        ctx = self.ctx
        if po.capture_status in ("DECLINED", "FAILED"):
            return self._fail(request_id, f"CAPTURE_{po.capture_status}")
        if po.capture_status != "COMPLETED":
            if po.capture_status == "PENDING":
                self._note_status(request_id, "CAPTURE_PENDING")
                return self._view(request_id)
            return self._unknown(request_id, "CAPTURE_STATUS_UNCLEAR", f"capture_status={po.capture_status}")

        with ctx.db.transaction() as s:
            req = ctx.load_request(s, request_id)
            if req.state == State.EXECUTED.value:
                return request_view(req, ctx.grant_for(s, req.id))  # already finalized (idempotent)
            if req.state not in LIVE:
                ctx.audit(s, "CAPTURE_AFTER_TERMINAL_STATE", "gateway", req.id, state=req.state,
                          capture_id=po.capture_id)
                return request_view(req, ctx.grant_for(s, req.id))
            if po.captured_amount_minor != req.amount_minor or (po.captured_currency or "").upper() != req.currency:
                # Money moved, but not what was authorized: hold for manual review, never auto-release.
                if req.state == State.EXECUTING.value:
                    transition(s, req, State.UNKNOWN, ctx.clock)
                req.failure_reason = "CAPTURE_AMOUNT_MISMATCH"
                ctx.audit(s, "CAPTURE_MISMATCH", "gateway", req.id, captured_minor=po.captured_amount_minor,
                          captured_currency=po.captured_currency, authorized_minor=req.amount_minor)
                return request_view(req, ctx.grant_for(s, req.id))
            transition(s, req, State.EXECUTED, ctx.clock)
            settled = ctx.budget.settle(s, req.id)
            req.provider_capture_id = po.capture_id
            req.provider_status = "COMPLETED"
            req.failure_reason = None
            ctx.audit(s, "PAYMENT_CAPTURED", "gateway", req.id, provider=req.provider,
                      provider_order_id=req.provider_order_id, capture_id=po.capture_id,
                      amount_minor=po.captured_amount_minor, currency=po.captured_currency,
                      reservations_settled=settled)
            order_id = req.order_id
        self._merchant(order_id, "CAPTURED", authorization_id=request_id, provider_ref=po.capture_id)
        return self._view(request_id)

    # ------------------------------------------------------------------ buyer redirects
    def request_for_provider_order(self, provider_order_id: str) -> str:
        with self.ctx.db.transaction() as s:
            rid = s.execute(select(AuthorizationRequest.id).where(
                AuthorizationRequest.provider_order_id == provider_order_id)).scalar_one_or_none()
        if rid is None:
            raise NotFound("PROVIDER_ORDER_UNKNOWN", "No authorization for this provider order")
        return rid

    def handle_provider_return(self, provider_order_id: str) -> dict[str, Any]:
        """Buyer approved at the provider: the backend captures (spec §9.1 step 7)."""
        return self._capture(self.request_for_provider_order(provider_order_id))

    def handle_provider_cancel(self, provider_order_id: str) -> dict[str, Any]:
        """Buyer cancelled. Reconcile first (never release funds that might have been captured)."""
        ctx = self.ctx
        rid = self.request_for_provider_order(provider_order_id)
        with ctx.db.transaction() as s:
            req = ctx.load_request(s, rid)
            if req.state not in LIVE:
                return request_view(req, ctx.grant_for(s, req.id))
            adapter = ctx.adapters[req.provider]
        try:
            po = adapter.get_order(provider_order_id)
        except ProviderTimeout:
            return self._cancel_unverified(rid)
        except ProviderError as exc:
            if exc.code not in NOT_FOUND:
                return self._cancel_unverified(rid)
            if self._not_found_after_unknown_capture(rid, exc.code):
                return self._view(rid)
            po = None  # no order at the provider, so nothing was captured
        if po is not None and po.status == "COMPLETED":
            return self._finalize(rid, po)
        return self._fail(rid, "BUYER_CANCELLED", merchant_status="CANCELLED")

    def _cancel_unverified(self, request_id: str) -> dict[str, Any]:
        if self._capture_outcome_unknown(request_id):
            return self._view(request_id)  # keep PROVIDER_TIMEOUT_ON_CAPTURE: it guards against a later 404
        return self._unknown(request_id, "CANCEL_UNVERIFIED", "could not confirm provider state after buyer cancel")

    def abandon(self, request_id: str, actor: str) -> dict[str, Any]:
        """Operator cancels a payment stuck in checkout (e.g. the buyer gave up). The provider is asked first:
        a captured payment is finalized, never cancelled; otherwise the request fails and funds are released."""
        ctx = self.ctx
        with ctx.db.transaction() as s:
            req = ctx.load_request(s, request_id)
            if req.state not in LIVE:
                raise Conflict("NOT_EXECUTING", f"Request is {req.state}; only EXECUTING/UNKNOWN payments can be cancelled")
            poid = req.provider_order_id
        if poid is None:
            view = self.reconcile(request_id)  # resolve an unknown create first (idempotent)
            poid = view["execution"]["provider_order_id"]
            if view["state"] not in LIVE:
                return view
        with ctx.db.transaction() as s:
            req = ctx.load_request(s, request_id)
            adapter = ctx.adapters[req.provider]
        if poid is not None:
            try:
                po = adapter.get_order(poid)
            except (ProviderTimeout, ProviderError) as exc:
                gone = isinstance(exc, ProviderError) and exc.code in NOT_FOUND
                # Gone means no order, so no capture - unless a capture call's outcome is still unknown.
                if not gone or self._not_found_after_unknown_capture(request_id, exc.code):
                    raise Conflict("CANNOT_VERIFY_PROVIDER",
                                   f"Could not confirm the provider state ({type(exc).__name__}); try again later") from None
                po = None
            if po is not None and po.status == "COMPLETED":
                return self._finalize(request_id, po)
        with ctx.db.transaction() as s:
            ctx.audit(s, "CANCEL_REQUESTED", actor, request_id, provider_order_id=poid)
        return self._fail(request_id, "ABANDONED_CHECKOUT", merchant_status="CANCELLED")

    # ------------------------------------------------------------------ reconciliation
    def reconcile(self, request_id: str) -> dict[str, Any]:
        ctx = self.ctx
        with ctx.db.transaction() as s:
            req = ctx.load_request(s, request_id)
            if req.state not in LIVE:
                return request_view(req, ctx.grant_for(s, req.id))
            adapter, poid = ctx.adapters[req.provider], req.provider_order_id
            if poid is None:
                # The create call's outcome is unknown: retry it with the SAME idempotency key, so the
                # provider returns the original order instead of creating a second one.
                if req.state == State.UNKNOWN.value:
                    transition(s, req, State.EXECUTING, ctx.clock)
                ctx.audit(s, "RECONCILED", "gateway", req.id, action="retry_create_idempotent")
        if poid is None:
            return self._create_provider_order(request_id)
        return self._reconcile_with(request_id, adapter, poid)

    def _reconcile_with(self, request_id: str, adapter, poid: str) -> dict[str, Any]:
        ctx = self.ctx
        try:
            po = adapter.get_order(poid)
        except ProviderTimeout:
            return self._view(request_id)
        except ProviderError as exc:
            if exc.code in NOT_FOUND and not self._not_found_after_unknown_capture(request_id, exc.code):
                return self._fail(request_id, "PROVIDER_ORDER_NOT_FOUND")
            return self._view(request_id)
        with ctx.db.transaction() as s:
            ctx.audit(s, "RECONCILED", "gateway", request_id, provider_status=po.status,
                      capture_status=po.capture_status)
        if po.status == "COMPLETED":
            return self._finalize(request_id, po)
        if po.status == "APPROVED":
            return self._capture(request_id)
        if po.status in ("CREATED", "PAYER_ACTION_REQUIRED"):
            with ctx.db.transaction() as s:
                req = ctx.load_request(s, request_id)
                if req.state == State.UNKNOWN.value:
                    transition(s, req, State.EXECUTING, ctx.clock)
                req.provider_status = po.status
                if po.approval_url:
                    req.approval_url = po.approval_url
            return self._view(request_id)
        if po.status == "VOIDED":
            return self._fail(request_id, "PROVIDER_ORDER_VOIDED")
        return self._view(request_id)

    # ------------------------------------------------------------------ state helpers
    def _capture_outcome_unknown(self, request_id: str) -> bool:
        with self.ctx.db.transaction() as s:
            return self.ctx.load_request(s, request_id).failure_reason == CAPTURE_OUTCOME_UNKNOWN

    def _not_found_after_unknown_capture(self, request_id: str, code: str) -> bool:
        """True when a capture call had an unknown outcome, so "order not found" must not release funds:
        the request stays UNKNOWN for manual review and the 404 is audited (once, not on every poll)."""
        ctx = self.ctx
        with ctx.db.transaction() as s:
            req = ctx.load_request(s, request_id)
            if req.failure_reason != CAPTURE_OUTCOME_UNKNOWN:
                return False
            if req.provider_status != code:
                req.provider_status = code
                ctx.audit(s, "RECONCILED", "gateway", req.id, action="hold_unknown_capture_order_not_found",
                          provider_code=code, provider_order_id=req.provider_order_id)
            return True

    def _unknown(self, request_id: str, reason: str, detail: str) -> dict[str, Any]:
        ctx = self.ctx
        with ctx.db.transaction() as s:
            req = ctx.load_request(s, request_id)
            if req.state == State.EXECUTING.value:
                transition(s, req, State.UNKNOWN, ctx.clock)
            if req.state == State.UNKNOWN.value:
                req.failure_reason = reason
                ctx.audit(s, "PROVIDER_TIMEOUT", "gateway", req.id, reason=reason, detail=detail[:200])
            return request_view(req, ctx.grant_for(s, req.id))

    def _fail(self, request_id: str, reason: str, *, merchant_status: str | None = "FAILED") -> dict[str, Any]:
        """Terminal failure. Only called once the provider state is known not to include a capture."""
        ctx = self.ctx
        with ctx.db.transaction() as s:
            req = ctx.load_request(s, request_id)
            if req.state not in LIVE:
                return request_view(req, ctx.grant_for(s, req.id))
            transition(s, req, State.FAILED, ctx.clock)
            released = ctx.budget.release(s, req.id)
            req.failure_reason = reason
            ctx.audit(s, "EXECUTION_FAILED", "gateway", req.id, reason=reason, reservations_released=released)
            order_id = req.order_id
        if merchant_status:
            self._merchant(order_id, merchant_status, authorization_id=request_id)
        return self._view(request_id)

    def _note_status(self, request_id: str, status: str) -> None:
        with self.ctx.db.transaction() as s:
            req = self.ctx.load_request(s, request_id)
            req.provider_status = status
            self.ctx.audit(s, "CAPTURE_NOT_COMPLETED", "gateway", req.id, provider_status=status)

    def _merchant(self, order_id: str, status: str, **kwargs) -> None:
        """Mirror the payment status to Merchant Lab. Failures are audited, never allowed to undo PACT state."""
        try:
            self.ctx.orders.set_payment_status(order_id, status, **kwargs)
        except PactError as exc:
            with self.ctx.db.transaction() as s:
                self.ctx.audit(s, "MERCHANT_SYNC_FAILED", "gateway", kwargs.get("authorization_id"),
                               order_id=order_id, status=status, code=exc.code)

    def _view(self, request_id: str) -> dict[str, Any]:
        with self.ctx.db.transaction() as s:
            req = self.ctx.load_request(s, request_id)
            return request_view(req, self.ctx.grant_for(s, req.id))
