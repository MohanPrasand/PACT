"""Authorization lifecycle state machine (spec §7.4). Every state change goes through ``transition``."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from sqlalchemy import update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from pact_core.db import Clock
from pact_core.errors import InvalidTransition
from pact_core.models import AuthorizationRequest, State

# Read-only so no caller can widen the lifecycle at runtime.
TRANSITIONS: Mapping[State, frozenset[State]] = MappingProxyType({
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
})

TERMINAL = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)
# States in which a request holds (or may still hold) a budget reservation.
RESERVING = frozenset({State.PENDING_APPROVAL, State.APPROVED, State.GRANTED, State.EXECUTING, State.UNKNOWN})


def _coerce(value: str) -> State | None:
    try:
        return State(value)
    except ValueError:
        return None


def can_transition(src: str, dst: str) -> bool:
    """True if ``src -> dst`` is a legal lifecycle edge. Unknown state names are never legal."""
    s, d = _coerce(src), _coerce(dst)
    return s is not None and d is not None and d in TRANSITIONS[s]


def transition(session: Session, req: AuthorizationRequest, dst: State, clock: Clock) -> None:
    """Compare-and-set state change: ``UPDATE ... WHERE id=? AND state=<src>``.

    ``src`` is the state of the in-memory ``req``. Raises ``InvalidTransition`` with code
    ``INVALID_STATE_TRANSITION`` if ``src -> dst`` is not a lifecycle edge, or
    ``CONCURRENT_STATE_CHANGE`` if the persisted row is no longer in ``src`` (another
    transaction moved it, so ``req`` is stale). On success ``req`` reflects the new state.
    """
    src, target = _coerce(req.state), _coerce(dst)
    if src is None or target is None or target not in TRANSITIONS[src]:
        raise InvalidTransition("INVALID_STATE_TRANSITION", f"Cannot move request {req.id} from {req.state} to {dst}")
    now = clock()
    result = session.execute(
        update(AuthorizationRequest)
        .where(AuthorizationRequest.id == req.id, AuthorizationRequest.state == src.value)
        .values(state=target.value, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise InvalidTransition("CONCURRENT_STATE_CHANGE", f"Request {req.id} changed state concurrently")
    # Mirror the persisted values without marking the object dirty (avoids a redundant UPDATE at flush).
    set_committed_value(req, "state", target.value)
    set_committed_value(req, "updated_at", now)
