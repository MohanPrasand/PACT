"""Grant manager (spec §7.5, §12): transaction-bound, single-use execution grants.

A grant is the permission an agent presents to execute exactly one approved transaction.
It is bound to the request's agent, user, merchant, order, action, amount, currency and
request hash, it expires, and it is consumed atomically by a conditional UPDATE.

The plaintext token is returned once, by ``issue``. Only ``sha256(token)`` is stored, and the
token is never logged or put into an error message.

This module neither writes audit events nor changes request state. The authorization service
(#8) and the execution gateway (#10) do that, so audit and state changes stay in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from pact_core import crypto
from pact_core.db import Clock
from pact_core.errors import Conflict, Forbidden, NotFound
from pact_core.models import AuthorizationRequest, Grant, State

TOKEN_PREFIX = "pact_gr"
GRANT_ID_PREFIX = "gr"


@dataclass(frozen=True)
class GrantClaim:
    """What the executing agent presents. ``agent_id`` is the authenticated caller, not self-declared."""

    request_id: str
    token: str
    agent_id: str
    merchant_id: str
    order_id: str
    amount_minor: int
    currency: str

    def __repr__(self) -> str:  # keep the token out of logs and tracebacks
        return (
            f"GrantClaim(request_id={self.request_id!r}, token='***', agent_id={self.agent_id!r}, "
            f"merchant_id={self.merchant_id!r}, order_id={self.order_id!r}, "
            f"amount_minor={self.amount_minor!r}, currency={self.currency!r})"
        )

    __str__ = __repr__


class GrantManager:
    def __init__(self, clock: Clock, ttl_seconds: int):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.clock = clock
        self.ttl = timedelta(seconds=ttl_seconds)

    # --- issue ---------------------------------------------------------------------------------

    def issue(self, s: Session, req: AuthorizationRequest) -> tuple[Grant, str]:
        """Create the grant for an APPROVED request; returns ``(grant, plaintext token)``.

        Only ``sha256(token)`` is stored. The request's state is not changed.
        """
        if req.state != State.APPROVED:
            raise Conflict(
                "GRANT_REQUEST_NOT_APPROVED",
                f"Request {req.id} is {req.state}; a grant can only be issued for an APPROVED request",
            )
        if s.scalar(select(Grant.id).where(Grant.request_id == req.id)) is not None:
            raise Conflict("GRANT_ALREADY_ISSUED", f"A grant was already issued for request {req.id}")

        token = crypto.new_secret(TOKEN_PREFIX)
        now = self.clock()
        grant = Grant(
            id=crypto.new_id(GRANT_ID_PREFIX),
            request_id=req.id,
            token_hash=crypto.sha256_hex(token),
            agent_id=req.agent_id,
            user_id=req.user_id,
            merchant_id=req.merchant_id,
            order_id=req.order_id,
            action_type=req.action_type,
            amount_minor=req.amount_minor,
            currency=req.currency,
            request_hash=req.request_hash,
            issued_at=now,
            expires_at=now + self.ttl,
        )
        try:
            with s.begin_nested():  # unique(request_id) backs the check above under concurrency
                s.add(grant)
        except IntegrityError:
            raise Conflict("GRANT_ALREADY_ISSUED", f"A grant was already issued for request {req.id}") from None
        return grant, token

    # --- validate ------------------------------------------------------------------------------

    def validate(self, s: Session, claim: GrantClaim, *, current_request_hash: str) -> Grant:
        """Run every grant check in the contract order, without mutating anything.

        ``current_request_hash`` is recomputed by the caller from the live, authoritative order.
        """
        grant = s.scalar(select(Grant).where(Grant.request_id == claim.request_id))
        if grant is None:
            raise NotFound("GRANT_NOT_FOUND", f"No grant for request {claim.request_id}")

        if not crypto.constant_time_equal(crypto.sha256_hex(claim.token), grant.token_hash):
            raise Forbidden("GRANT_INVALID", "Grant token is invalid")

        if claim.agent_id != grant.agent_id:
            raise Forbidden("GRANT_AGENT_MISMATCH", "Grant was issued to a different agent")

        if grant.revoked_at is not None:
            raise Conflict("GRANT_REVOKED", "Grant has been revoked")

        if grant.consumed_at is not None:
            raise Conflict("GRANT_ALREADY_USED", "Grant has already been used")

        if grant.expires_at <= self.clock():
            raise Conflict("GRANT_EXPIRED", "Grant has expired")

        field = _first_binding_mismatch(grant, claim)
        if field is not None:
            raise Conflict(
                "GRANT_BINDING_MISMATCH",
                f"Execute call does not match the grant ({field})",
                details={"field": field},
            )

        row_hash = s.scalar(select(AuthorizationRequest.request_hash).where(AuthorizationRequest.id == grant.request_id))
        if row_hash is None or not crypto.constant_time_equal(grant.request_hash, row_hash):
            raise Conflict("REQUEST_HASH_MISMATCH", "Grant does not match the authorization request")

        if not crypto.constant_time_equal(grant.request_hash, current_request_hash):
            raise Conflict("ORDER_MODIFIED", "The merchant order changed after authorization")

        return grant

    # --- consume -------------------------------------------------------------------------------

    def consume(self, s: Session, grant: Grant) -> None:
        """Atomically mark the grant used. Exactly one concurrent caller can succeed."""
        now = self.clock()
        result = s.execute(
            update(Grant)
            .where(
                Grant.id == grant.id,
                Grant.consumed_at.is_(None),
                Grant.revoked_at.is_(None),
                Grant.expires_at > now,
            )
            .values(consumed_at=now)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            # Mirror the persisted value without marking the object dirty.
            set_committed_value(grant, "consumed_at", now)
            return

        # Lost: read the current row (same transaction) to report the precise reason.
        row = s.execute(
            select(Grant.consumed_at, Grant.revoked_at, Grant.expires_at).where(Grant.id == grant.id)
        ).one_or_none()
        if row is None:
            raise NotFound("GRANT_NOT_FOUND", f"Grant {grant.id} does not exist")
        if row.revoked_at is not None:
            raise Conflict("GRANT_REVOKED", "Grant has been revoked")
        if row.consumed_at is None and row.expires_at <= now:
            raise Conflict("GRANT_EXPIRED", "Grant has expired")
        raise Conflict("GRANT_ALREADY_USED", "Grant has already been used")

    # --- revoke --------------------------------------------------------------------------------

    def revoke(self, s: Session, request_id: str, actor: str) -> bool:
        """Revoke the request's unused grant. False if there is none, or it is consumed or already revoked."""
        now = self.clock()
        result = s.execute(
            update(Grant)
            .where(Grant.request_id == request_id, Grant.consumed_at.is_(None), Grant.revoked_at.is_(None))
            .values(revoked_at=now, revoked_by=actor)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return False
        # Keep an already-loaded Grant for this request consistent with the row.
        for obj in s.identity_map.values():
            if isinstance(obj, Grant) and obj.request_id == request_id:
                set_committed_value(obj, "revoked_at", now)
                set_committed_value(obj, "revoked_by", actor)
        return True

    # --- expiry sweep --------------------------------------------------------------------------

    def find_expired_unconsumed(self, s: Session) -> list[Grant]:
        """Grants past ``expires_at`` that were neither consumed nor revoked, oldest first."""
        now = self.clock()
        return list(
            s.scalars(
                select(Grant)
                .where(Grant.expires_at <= now, Grant.consumed_at.is_(None), Grant.revoked_at.is_(None))
                .order_by(Grant.expires_at, Grant.id)
            )
        )


def _first_binding_mismatch(grant: Grant, claim: GrantClaim) -> str | None:
    if claim.merchant_id != grant.merchant_id:
        return "merchant"
    if claim.order_id != grant.order_id:
        return "order"
    if type(claim.amount_minor) is not int or claim.amount_minor != grant.amount_minor:
        return "amount"
    if not isinstance(claim.currency, str) or claim.currency.upper() != grant.currency.upper():
        return "currency"
    return None
