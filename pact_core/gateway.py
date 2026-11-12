"""Execution gateway (#10): the only code path that calls a payment adapter.

execute():  validate grant + bindings + live order + reservation  →  atomically consume the grant and
            move GRANTED → EXECUTING (one transaction)  →  create the provider order (outside it).
Buyer approves at the provider  →  handle_provider_return() captures  →  verify  →  EXECUTED + settle.

Ambiguous provider outcomes (timeout, 5xx, dropped connection) move the request to UNKNOWN. It is left
only through reconcile(), which asks the provider what happened. Payments are never blindly retried;
provider calls carry deterministic idempotency keys (``pact-<request>-create|capture``). The capture key
moves on only after the provider definitively declined an attempt (see ``_capture_key``).

Checkout lifecycle: the buyer's cancel link is not terminal (the order is still payable), an explicit
"cancel this payment" or an operator cancel is, and a checkout left open past ``checkout_ttl_seconds`` is
timed out by the sweeper. Every terminal path asks the provider first and never releases funds that may
have been captured.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import select

from pact_core.crypto import constant_time_equal, request_hash, sha256_hex
from pact_core.errors import Conflict, NotFound, PactError
from pact_core.grants import GrantClaim
from pact_core.models import AuditEvent, AuthorizationRequest, State
from pact_core.ports import CreatePayment, ProviderError, ProviderOrder, ProviderTimeout
from pact_core.runtime import AgentIdentity, PactContext, request_view
from pact_core.state_machine import transition

LIVE = (State.EXECUTING.value, State.UNKNOWN.value)
NOT_READY = {"ORDER_NOT_APPROVED", "PAYER_ACTION_REQUIRED"}
BUYER_RETRY = {"INSTRUMENT_DECLINED"}
# "No such order" from the provider: the simulation adapter's code and PayPal's 404 issue (#38).
NOT_FOUND = {"RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID"}
CAPTURE_OUTCOME_UNKNOWN = "PROVIDER_TIMEOUT_ON_CAPTURE"
# Set (durably, before the provider is called) while a capture call is being made, so that nothing can
# release the reservation between "capture sent" and "capture answered".
CAPTURE_IN_PROGRESS = "CAPTURE_IN_PROGRESS"
# A capture call whose outcome PACT does not know yet. Only a definitive provider answer clears these.
CAPTURE_ATTEMPT_OPEN = {CAPTURE_OUTCOME_UNKNOWN, CAPTURE_IN_PROGRESS}
# The provider showed a capture that PACT could not settle.
CAPTURE_SEEN = {"CAPTURE_AMOUNT_MISMATCH", "CAPTURE_STATUS_UNCLEAR"}
# After a declined funding source, a status poll may retry the capture by itself this often (the buyer may
# have chosen another source without coming back through the return URL). The buyer's return always retries.
DECLINE_POLL_RETRY_SECONDS = 60
DECLINE_POLL_RETRIES = 3
# The provider is still waiting for the buyer.
AWAITING_BUYER = {"CREATED", "PAYER_ACTION_REQUIRED"}
BUYER_CANCELLED = "BUYER_CANCELLED"
# provider_status values PACT sets itself while the request stays EXECUTING and the buyer may retry at the
# same approval_url. A provider answer of "still waiting for the buyer" must not overwrite them.
BUYER_MAY_RETRY = BUYER_RETRY | {BUYER_CANCELLED}


def _in_doubt(req: AuthorizationRequest) -> bool:
    """Money may have moved, or may be about to: a capture call is open, or the provider showed a capture
    PACT could not settle. While this holds, no path may release the reservation on indirect evidence
    (an order lookup, "order not found", a cancel or a timeout)."""
    return (req.failure_reason in CAPTURE_ATTEMPT_OPEN | CAPTURE_SEEN) or req.provider_status == "CAPTURE_PENDING"


def _captured(po: ProviderOrder) -> bool:
    """The provider reports a capture (in any state) for this order: only _finalize may decide."""
    return po.status == "COMPLETED" or bool(po.capture_id or po.capture_status)


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
        self._lock = threading.Lock()
        self._capturing: dict[str, int] = {}  # request id -> capture calls in flight in this process
        self._held_not_found: set[str] = set()  # requests whose held "order not found" was already audited

    def _is_capturing(self, request_id: str) -> bool:
        with self._lock:
            return self._capturing.get(request_id, 0) > 0

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
            key = self._capture_key(s, request_id)
            # Durable marker, committed before the provider is called: from here until a definitive answer,
            # _fail refuses to release the reservation on anyone else's evidence (cancel, timeout, 404).
            if req.failure_reason not in CAPTURE_ATTEMPT_OPEN | CAPTURE_SEEN:
                req.failure_reason = CAPTURE_IN_PROGRESS
        with self._lock:
            self._capturing[request_id] = self._capturing.get(request_id, 0) + 1
        try:
            try:
                po = adapter.capture_order(poid, key)
            except ProviderTimeout as exc:
                return self._unknown(request_id, CAPTURE_OUTCOME_UNKNOWN, str(exc))
            except ProviderError as exc:
                if exc.code in NOT_READY | BUYER_RETRY:
                    self._capture_answered(request_id)  # definitive: nothing was captured under this key
                    self._note_status(request_id, exc.code)
                    return self._view(request_id)
                return self._after_capture_rejected(request_id, adapter, poid, exc)
            return self._finalize(request_id, po)
        finally:
            with self._lock:
                left = self._capturing.get(request_id, 1) - 1
                if left > 0:
                    self._capturing[request_id] = left
                else:
                    self._capturing.pop(request_id, None)

    def _capture_answered(self, request_id: str) -> None:
        """The provider answered definitively that nothing was captured under the current key (not approved,
        declined, rejected). That also answers an earlier call with the same key whose outcome was unknown,
        so the open-capture marker is cleared and an UNKNOWN request goes back to EXECUTING."""
        ctx = self.ctx
        with ctx.db.transaction() as s:
            req = ctx.load_request(s, request_id)
            if req.state in LIVE and req.failure_reason in CAPTURE_ATTEMPT_OPEN:
                req.failure_reason = None
                if req.state == State.UNKNOWN.value:
                    transition(s, req, State.EXECUTING, ctx.clock)

    def _capture_call_did_nothing(self, request_id: str) -> None:
        """This capture call had no effect (e.g. rate limited), but it does not answer an earlier unknown one."""
        with self.ctx.db.transaction() as s:
            req = self.ctx.load_request(s, request_id)
            if req.state in LIVE and req.failure_reason == CAPTURE_IN_PROGRESS:
                req.failure_reason = None

    def _decline_retry_due(self, s, request_id: str) -> bool:
        """After a decline, may a status poll retry the capture? Rarely and a bounded number of times."""
        rows = s.execute(select(AuditEvent.ts, AuditEvent.data).where(
            AuditEvent.request_id == request_id, AuditEvent.event_type == "CAPTURE_NOT_COMPLETED")).all()
        declines = [ts for ts, data in rows if (data or {}).get("provider_status") in BUYER_RETRY]
        if not declines or len(declines) > DECLINE_POLL_RETRIES:
            return False
        return self.ctx.clock() - max(declines) >= timedelta(seconds=DECLINE_POLL_RETRY_SECONDS)

    def _declines(self, s, request_id: str) -> int:
        """Audited definitive buyer-retry declines of a capture (CAPTURE_NOT_COMPLETED, BUYER_RETRY status)."""
        noted = s.execute(select(AuditEvent.data).where(
            AuditEvent.request_id == request_id, AuditEvent.event_type == "CAPTURE_NOT_COMPLETED")).scalars()
        return sum(1 for data in noted if (data or {}).get("provider_status") in BUYER_RETRY)

    def _capture_key(self, s, request_id: str) -> str:
        """Idempotency key for the next capture call (#41).

        ``pact-<request>-capture`` for the first attempt, ``pact-<request>-capture-<n>`` after ``n`` declines.
        A decline is a definitive "nothing was captured", so a fresh key lets the buyer's next attempt
        through instead of replaying the stored decline. The key never advances after a timeout or a
        not-ready answer: a retry after an unknown outcome reuses it, which is what makes that retry safe.
        Request ids are at most 40 characters, so the key stays inside the PayPal adapter's 108-character
        request-id limit."""
        n = self._declines(s, request_id)
        return f"pact-{request_id}-capture" + (f"-{n}" if n else "")

    def _after_capture_rejected(self, request_id: str, adapter, poid: str, exc: ProviderError) -> dict[str, Any]:
        """The provider refused this capture call. Look at the order once and decide; never capture again
        from here (#39: capture -> reconcile -> capture recursed without bound)."""
        try:
            po = adapter.get_order(poid)
        except (ProviderTimeout, ProviderError):
            self._capture_call_did_nothing(request_id)
            self._note_status(request_id, exc.code)  # cannot tell yet; the next poll retries
            return self._view(request_id)
        if _captured(po):
            return self._finalize(request_id, po)  # e.g. ORDER_ALREADY_CAPTURED: money moved earlier
        if exc.retryable:
            self._capture_call_did_nothing(request_id)
            self._note_status(request_id, exc.code)  # rate limit / transient auth: the next poll retries
            return self._view(request_id)
        self._capture_answered(request_id)  # refused outright, and the order shows no capture
        return self._fail(request_id, f"CAPTURE_REJECTED:{exc.code}")

    def _finalize(self, request_id: str, po: ProviderOrder) -> dict[str, Any]:
        ctx = self.ctx
        if po.capture_status in ("DECLINED", "FAILED"):
            # The provider's own verdict on the capture: definitive, whatever marker is set.
            return self._fail(request_id, f"CAPTURE_{po.capture_status}", definitive=True)
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
                if req.failure_reason != "CAPTURE_AMOUNT_MISMATCH":  # audit once, not on every poll
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
        """The buyer followed the provider's cancel link (#40). That is not terminal: the order is still
        payable, so the request stays EXECUTING with provider_status BUYER_CANCELLED and the buyer can go
        back to the same approval_url. The provider is asked first; a captured order is finalized."""
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
            # No order at the provider, so nothing was captured and there is nothing left to retry.
            return self._fail(rid, BUYER_CANCELLED, merchant_status="CANCELLED")
        if _captured(po):
            return self._finalize(rid, po)
        with ctx.db.transaction() as s:
            req = ctx.load_request(s, rid)
            unknown = req.state == State.UNKNOWN.value
            if req.state == State.EXECUTING.value and req.provider_status != BUYER_CANCELLED:
                req.provider_status = BUYER_CANCELLED
                ctx.audit(s, "CHECKOUT_CANCELLED_BY_BUYER", "buyer", req.id, provider_order_id=provider_order_id,
                          provider_order_status=po.status)
        if unknown:
            return self._reconcile_with(rid, adapter, provider_order_id)  # the only way out of UNKNOWN
        return self._view(rid)

    def _cancel_unverified(self, request_id: str) -> dict[str, Any]:
        """The provider could not be asked, so the cancel link taught PACT nothing: change nothing. (Moving
        to UNKNOWN here would let one unauthenticated GET park a payment where no timeout reaches it.)"""
        return self._view(request_id)

    def abandon(self, request_id: str, actor: str, *, force: bool = False) -> dict[str, Any]:
        """Operator cancels a payment stuck in checkout (e.g. the buyer gave up). The provider is asked first:
        a captured payment is finalized, never cancelled; otherwise the request fails and funds are released.

        ``force`` is the manual-review exit: for a payment PACT cannot verify (a capture may have happened
        and the provider no longer answers for it), an operator who has checked the provider account by
        hand closes it. It releases the reservation without provider evidence, is audited as CANCEL_FORCED,
        and is still refused while a capture call is in flight."""
        with self.ctx.db.transaction() as s:
            req = self.ctx.load_request(s, request_id)
            if req.state not in LIVE:
                raise Conflict("NOT_EXECUTING", f"Request is {req.state}; only EXECUTING/UNKNOWN payments can be cancelled")
            poid, doubt = req.provider_order_id, _in_doubt(req)
        if force:
            if self._is_capturing(request_id):
                raise Conflict("CANNOT_VERIFY_PROVIDER", "A capture for this payment is in progress; try again later")
            return self._fail(request_id, "FORCE_CANCELLED", merchant_status="CANCELLED", definitive=True,
                              note=("CANCEL_FORCED", actor, {"provider_order_id": poid, "capture_in_doubt": doubt}))
        return self._abandon(request_id, actor=actor, reason="ABANDONED_CHECKOUT", event="CANCEL_REQUESTED",
                             merchant_status="CANCELLED")

    def buyer_abandon(self, provider_order_id: str) -> dict[str, Any]:
        """The buyer explicitly gives up this payment (#40): the same provider-checked terminal path as
        ``abandon``. A request that is no longer in checkout returns its view, so a double submit is harmless."""
        return self._abandon(self.request_for_provider_order(provider_order_id), actor="buyer",
                             reason=BUYER_CANCELLED, event="CANCEL_REQUESTED", merchant_status="CANCELLED")

    def expire_checkout(self, request_id: str) -> dict[str, Any]:
        """Time out a checkout the buyer never finished (#42); called by the expiry sweep, outside any
        transaction. Only EXECUTING requests are touched: UNKNOWN ones, and any request with a capture call
        of unknown outcome, stay for reconciliation / manual review. A captured order is finalized and an
        approved one is captured instead of failed. If the provider cannot be asked, nothing changes and the
        next sweep tries again."""
        try:
            return self._abandon(request_id, actor="system", reason="CHECKOUT_TIMEOUT", event="CHECKOUT_TIMED_OUT",
                                 merchant_status="FAILED", timeout=True)
        except Conflict:
            return self._view(request_id)  # CANNOT_VERIFY_PROVIDER: leave it for the next sweep

    def _abandon(self, request_id: str, *, actor: str, reason: str, event: str, merchant_status: str,
                 timeout: bool = False) -> dict[str, Any]:
        """Provider-checked terminal path shared by the operator cancel, the buyer's explicit cancel and the
        checkout timeout. Funds are released only once the provider is known to hold no capture; when that
        cannot be established it raises CANNOT_VERIFY_PROVIDER and changes nothing."""
        ctx = self.ctx
        with ctx.db.transaction() as s:
            req = ctx.load_request(s, request_id)
            if req.state not in LIVE or (timeout and req.state != State.EXECUTING.value):
                return request_view(req, ctx.grant_for(s, req.id))
            poid = req.provider_order_id
            doubt, open_attempt = _in_doubt(req), req.failure_reason in CAPTURE_ATTEMPT_OPEN
        if doubt:
            # A capture may have happened. An order lookup cannot rule that out (it can lag behind a capture),
            # so only the provider's answer to the capture itself may clear this. Ask again with the same
            # key through reconcile; if that does not settle it, nothing is released.
            if open_attempt and not self._is_capturing(request_id):
                view = self.reconcile(request_id)
                if view["state"] not in LIVE or (timeout and view["state"] != State.EXECUTING.value):
                    return view
            with ctx.db.transaction() as s:
                req = ctx.load_request(s, request_id)
                still = _in_doubt(req)
                view = request_view(req, ctx.grant_for(s, req.id))
            if still:
                if timeout:
                    return view
                raise Conflict("CANNOT_VERIFY_PROVIDER",
                               "A capture for this payment may have happened and is not settled yet; try again later")
        if poid is None:
            view = self.reconcile(request_id)  # resolve an unknown create first (idempotent)
            poid = view["execution"]["provider_order_id"]
            if view["state"] not in LIVE or (timeout and view["state"] != State.EXECUTING.value):
                return view
        with ctx.db.transaction() as s:
            req = ctx.load_request(s, request_id)
            adapter = ctx.adapters[req.provider]
            declines = self._declines(s, request_id)
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
            if po is not None and _captured(po):
                return self._finalize(request_id, po)
            if timeout and po is not None and po.status == "APPROVED":
                # The buyer approved but never came back: take the payment rather than fail it.
                view = self._capture(request_id)
                with ctx.db.transaction() as s:
                    declined = self._declines(s, request_id) > declines
                if view["state"] != State.EXECUTING.value or not declined:
                    return view  # captured, failed, unknown or not ready: nothing more to decide here
                # Definitively declined just now (nothing captured) and the checkout window is over.
        view = self._fail(request_id, reason, merchant_status=merchant_status,
                          note=(event, actor, {"provider_order_id": poid}))
        if view["state"] in LIVE:  # a capture call started in the meantime: _fail refused to release
            raise Conflict("CANNOT_VERIFY_PROVIDER", "A capture for this payment is in progress; try again later")
        return view

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
        seen = "CAPTURE_PENDING" if po.capture_status == "PENDING" else po.status
        with ctx.db.transaction() as s:
            req = ctx.load_request(s, request_id)
            if req.state not in LIVE:
                return request_view(req, ctx.grant_for(s, req.id))
            unknown = req.state == State.UNKNOWN.value
            probe = req.failure_reason in CAPTURE_ATTEMPT_OPEN
            # The buyer cancelled or was declined and has not acted since: keep PACT's status (#40, #41).
            keep = req.provider_status in BUYER_MAY_RETRY and po.status in AWAITING_BUYER
            # Declined and still approved with the declined funding source: capturing again from a poll
            # would only repeat the decline. The buyer's return (or the checkout timeout) retries it.
            wait_for_buyer = (req.provider_status in BUYER_RETRY and po.status == "APPROVED" and not unknown
                              and not self._decline_retry_due(s, request_id))
            # Audit only what is new (#42): an unchanged poll writes nothing.
            if (seen != req.provider_status and not keep and not wait_for_buyer) or (unknown and po.status in AWAITING_BUYER):
                ctx.audit(s, "RECONCILED", "gateway", request_id, provider_status=po.status,
                          capture_status=po.capture_status)
        if po.status == "COMPLETED":
            return self._finalize(request_id, po)
        if po.status == "APPROVED":
            return self._view(request_id) if wait_for_buyer else self._capture(request_id)
        if po.status == "VOIDED":
            return self._fail(request_id, "PROVIDER_ORDER_VOIDED")
        if probe and po.status in AWAITING_BUYER and not self._is_capturing(request_id):
            # A capture call is still unanswered. This lookup says "not approved", but a lookup can lag behind
            # a capture, so it settles nothing: repeat the capture with the same key and let the provider
            # answer for that call (captured, or definitively not).
            return self._capture(request_id)
        with ctx.db.transaction() as s:
            req = ctx.load_request(s, request_id)
            if req.state in LIVE:
                if po.status in AWAITING_BUYER:
                    if req.state == State.UNKNOWN.value:
                        transition(s, req, State.EXECUTING, ctx.clock)
                    if req.failure_reason == "CANCEL_UNVERIFIED":  # legacy marker; the provider has now answered
                        req.failure_reason = None
                    if po.approval_url:
                        req.approval_url = po.approval_url
                if not keep:
                    req.provider_status = po.status
        return self._view(request_id)

    # ------------------------------------------------------------------ state helpers
    def _not_found_after_unknown_capture(self, request_id: str, code: str) -> bool:
        """True when a capture may have happened (open capture call, pending capture, or a capture PACT
        could not settle), so "order not found" must not release funds: the request is held for
        reconciliation / manual review and the 404 is audited once."""
        ctx = self.ctx
        with ctx.db.transaction() as s:
            req = ctx.load_request(s, request_id)
            if not _in_doubt(req):
                return False
            with self._lock:
                first = request_id not in self._held_not_found
                self._held_not_found.add(request_id)
            if first:
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
            else:  # already terminal: never lose the trace of a call that may have moved money
                ctx.audit(s, "PROVIDER_TIMEOUT", "gateway", req.id, reason=reason, detail=detail[:200],
                          state=req.state, after_terminal_state=True)
            return request_view(req, ctx.grant_for(s, req.id))

    def _fail(self, request_id: str, reason: str, *, merchant_status: str | None = "FAILED",
              note: tuple[str, str, dict[str, Any]] | None = None, definitive: bool = False) -> dict[str, Any]:
        """Terminal failure. Only called once the provider state is known not to include a capture.
        ``note`` is an ``(event, actor, data)`` audit record written with the failure, only if it happens.

        Last line of defence: unless the caller holds the provider's own verdict on the capture
        (``definitive``), a request with a capture in doubt is NOT failed and nothing is released; the
        unchanged view is returned. This check runs in the same transaction as the release, and _capture
        commits its marker before calling the provider, so a cancel or timeout can never overtake a capture."""
        ctx = self.ctx
        with ctx.db.transaction() as s:
            req = ctx.load_request(s, request_id)
            if req.state not in LIVE:
                return request_view(req, ctx.grant_for(s, req.id))
            if not definitive and _in_doubt(req):
                ctx.audit(s, "EXECUTION_REJECTED", "gateway", req.id, code="CAPTURE_IN_DOUBT",
                          message=f"refused to fail ({reason}) while a capture may have happened")
                return request_view(req, ctx.grant_for(s, req.id))
            transition(s, req, State.FAILED, ctx.clock)
            released = ctx.budget.release(s, req.id)
            req.failure_reason = reason
            if note is not None:
                ctx.audit(s, note[0], note[1], req.id, **note[2])
            ctx.audit(s, "EXECUTION_FAILED", "gateway", req.id, reason=reason, reservations_released=released)
            order_id = req.order_id
        if merchant_status:
            self._merchant(order_id, merchant_status, authorization_id=request_id)
        return self._view(request_id)

    def _note_status(self, request_id: str, status: str) -> None:
        with self.ctx.db.transaction() as s:
            req = self.ctx.load_request(s, request_id)
            if status in NOT_READY and req.provider_status in BUYER_MAY_RETRY:
                return  # "not approved yet" is no news after a cancel or decline: keep that status
            # Every buyer-retry decline is audited (the capture key counts them); anything else only when
            # it is news, so repeated polls of e.g. a pending capture do not fill the log.
            if req.provider_status == status and status not in BUYER_RETRY:
                return
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
