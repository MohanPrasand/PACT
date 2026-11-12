"""Authorization lifecycle state machine. Every state change goes through ``transition``."""

from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.orm import Session

from pact_core.db import Clock
from pact_core.errors import InvalidTransition
from pact_core.models import AuthorizationRequest, State

TRANSITIONS: dict[State, frozenset[State]] = {
    State.CREATED: frozenset({State.EVALUATING}),
    State.EVALUATING: frozenset({State.DENIED, State.PENDING_APPROVAL, State.APPROVED}),
    State.PENDING_APPROVAL: frozenset({State.APPROVED, State.DENIED, State.EXPIRED, State.REVOKED}),
    State.APPROVED: frozenset({State.GRANTED, State.EXPIRED, State.REVOKED}),
    State.GRANTED: frozenset({State.EXECUTING, State.EXPIRED, State.REVOKED}),
    State.EXECUTING: frozenset({State.EXECUTED, State.FAILED, State.UNKNOWN}),
    # UNKNOWN is only left through provider reconciliation.
    State.UNKNOWN: frozenset({State.EXECUTING, State.EXECUTED, State.FAILED}),
    State.DENIED: frozenset(),
    State.EXECUTED: frozenset(),
    State.FAILED: frozenset(),
    State.EXPIRED: frozenset(),
    State.REVOKED: frozenset(),
}

TERMINAL = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)
# States in which a request holds (or may still hold) a budget reservation.
RESERVING = frozenset({State.PENDING_APPROVAL, State.APPROVED, State.GRANTED, State.EXECUTING, State.UNKNOWN})


def can_transition(src: str, dst: str) -> bool:
    return State(dst) in TRANSITIONS[State(src)]


def transition(session: Session, req: AuthorizationRequest, dst: State, clock: Clock) -> None:
    """Compare-and-set state change. Fails if the transition is illegal or the row moved underneath us."""
    src = State(req.state)
    if dst not in TRANSITIONS[src]:
        raise InvalidTransition("INVALID_STATE_TRANSITION", f"Cannot move request {req.id} from {src} to {dst}")
    now = clock()
    result = session.execute(
        update(AuthorizationRequest)
        .where(AuthorizationRequest.id == req.id, AuthorizationRequest.state == src.value)
        .values(state=dst.value, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise InvalidTransition("CONCURRENT_STATE_CHANGE", f"Request {req.id} changed state concurrently")
    req.state = dst.value
    req.updated_at = now
