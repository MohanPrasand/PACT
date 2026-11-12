"""#3 Authorization state machine (spec §7.4)."""

from __future__ import annotations

import itertools
import threading
from datetime import datetime

import pytest
from sqlalchemy import select, update

from pact_core.errors import InvalidTransition
from pact_core.models import Agent, AuthorizationRequest, State
from pact_core.state_machine import (
    RESERVING,
    TERMINAL,
    TRANSITIONS,
    can_transition,
    transition,
)

# The lifecycle from issue #3, written out independently of the implementation's table.
EXPECTED_EDGES: dict[State, set[State]] = {
    State.CREATED: {State.EVALUATING},
    State.EVALUATING: {State.DENIED, State.PENDING_APPROVAL, State.APPROVED},
    State.PENDING_APPROVAL: {State.APPROVED, State.DENIED, State.EXPIRED, State.REVOKED},
    State.APPROVED: {State.GRANTED, State.EXPIRED, State.REVOKED},
    State.GRANTED: {State.EXECUTING, State.EXPIRED, State.REVOKED},
    State.EXECUTING: {State.EXECUTED, State.FAILED, State.UNKNOWN},
    State.UNKNOWN: {State.EXECUTING, State.EXECUTED, State.FAILED},
}
EXPECTED_TERMINAL = {State.DENIED, State.EXECUTED, State.FAILED, State.EXPIRED, State.REVOKED}

ALL_PAIRS = list(itertools.product(State, State))
ALLOWED = [(s, d) for s, d in ALL_PAIRS if d in EXPECTED_EDGES.get(s, set())]
FORBIDDEN = [(s, d) for s, d in ALL_PAIRS if d not in EXPECTED_EDGES.get(s, set())]
CREATED_AT = datetime(2026, 1, 1, 0, 0, 0)

_ids = itertools.count()


def _pair_id(pair: tuple[State, State]) -> str:
    return f"{pair[0].value}->{pair[1].value}"


def make_request(db, state: State) -> str:
    """Insert an Agent + AuthorizationRequest in ``state`` and return the request id."""
    n = next(_ids)
    with db.transaction() as s:
        agent = Agent(id=f"ag_{n}", name="test agent", user_id="user_1", api_key_hash=f"{n:064d}")
        s.add(agent)
        s.add(
            AuthorizationRequest(
                id=f"req_{n}",
                agent_id=agent.id,
                user_id="user_1",
                merchant_id="merchant_1",
                order_id=f"order_{n}",
                action_type="purchase",
                amount_minor=1999,
                currency="USD",
                request_hash="a" * 64,
                state=state.value,
                created_at=CREATED_AT,
                updated_at=CREATED_AT,
            )
        )
    return f"req_{n}"


def load(session, req_id: str) -> AuthorizationRequest:
    return session.get(AuthorizationRequest, req_id)


def persisted(db, req_id: str) -> tuple[str, datetime]:
    """Re-read the row in a fresh transaction (no identity-map caching)."""
    with db.transaction() as s:
        row = s.execute(
            select(AuthorizationRequest.state, AuthorizationRequest.updated_at).where(AuthorizationRequest.id == req_id)
        ).one()
        return row.state, row.updated_at


def _run_threads(n: int, target) -> list[BaseException]:
    errors: list[BaseException] = []
    start = threading.Barrier(n)

    def wrapped(i):
        try:
            start.wait()
            target(i)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test
            errors.append(exc)

    threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return errors


# --- Table consistency ------------------------------------------------------------------------


def test_transitions_match_issue_lifecycle():
    assert {s: set(d) for s, d in TRANSITIONS.items() if d} == EXPECTED_EDGES


def test_every_state_has_a_transitions_entry():
    assert set(TRANSITIONS) == set(State)


def test_terminal_is_exactly_states_without_outgoing_edges():
    assert TERMINAL == {s for s, d in TRANSITIONS.items() if not d}
    assert TERMINAL == EXPECTED_TERMINAL


def test_reserving_states():
    assert RESERVING == {State.PENDING_APPROVAL, State.APPROVED, State.GRANTED, State.EXECUTING, State.UNKNOWN}
    assert not RESERVING & TERMINAL
    assert State.CREATED not in RESERVING and State.EVALUATING not in RESERVING


def test_no_self_loops():
    assert all(s not in d for s, d in TRANSITIONS.items())


def test_transitions_table_is_read_only():
    with pytest.raises(TypeError):
        TRANSITIONS[State.DENIED] = frozenset({State.EXECUTING})  # type: ignore[index]


def test_every_state_reachable_from_created():
    seen, frontier = {State.CREATED}, [State.CREATED]
    while frontier:
        for nxt in TRANSITIONS[frontier.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    assert seen == set(State)


@pytest.mark.parametrize("pair", ALL_PAIRS, ids=_pair_id)
def test_can_transition_matches_matrix(pair):
    src, dst = pair
    assert can_transition(src.value, dst.value) is (pair in ALLOWED)


def test_can_transition_rejects_unknown_names():
    assert can_transition("BOGUS", "EXECUTING") is False
    assert can_transition("GRANTED", "BOGUS") is False


# --- Full state x state matrix against the database -------------------------------------------


@pytest.mark.parametrize("pair", ALLOWED, ids=_pair_id)
def test_allowed_transition_persists(db, clock, pair):
    src, dst = pair
    req_id = make_request(db, src)
    clock.advance(minutes=5)
    with db.transaction() as s:
        req = load(s, req_id)
        transition(s, req, dst, clock)
        assert req.state == dst.value
        assert req.updated_at == clock()
    assert persisted(db, req_id) == (dst.value, clock())


@pytest.mark.parametrize("pair", FORBIDDEN, ids=_pair_id)
def test_forbidden_transition_rejected_and_row_unchanged(db, clock, pair):
    src, dst = pair
    req_id = make_request(db, src)
    clock.advance(minutes=5)
    with db.transaction() as s:
        req = load(s, req_id)
        with pytest.raises(InvalidTransition) as exc:
            transition(s, req, dst, clock)
        assert exc.value.code == "INVALID_STATE_TRANSITION"
        assert exc.value.status_code == 409
        assert req.state == src.value
    assert persisted(db, req_id) == (src.value, CREATED_AT)


def test_matrix_covers_every_pair():
    assert len(ALLOWED) + len(FORBIDDEN) == len(State) ** 2
    assert len(ALLOWED) == sum(len(d) for d in TRANSITIONS.values())


# --- Explicit acceptance cases ----------------------------------------------------------------


def _assert_rejected(db, clock, src: State, dst: State) -> None:
    req_id = make_request(db, src)
    with db.transaction() as s, pytest.raises(InvalidTransition) as exc:
        transition(s, load(s, req_id), dst, clock)
    assert exc.value.code == "INVALID_STATE_TRANSITION"
    assert persisted(db, req_id)[0] == src.value


def test_denied_cannot_go_to_executing(db, clock):
    _assert_rejected(db, clock, State.DENIED, State.EXECUTING)


@pytest.mark.parametrize("terminal", sorted(EXPECTED_TERMINAL), ids=str)
def test_terminal_states_cannot_be_left(db, clock, terminal):
    for dst in State:
        _assert_rejected(db, clock, terminal, dst)


def test_granted_cannot_skip_to_executed(db, clock):
    _assert_rejected(db, clock, State.GRANTED, State.EXECUTED)


def test_unknown_cannot_be_expired_or_revoked(db, clock):
    """A possibly-captured payment must be reconciled, never silently abandoned."""
    _assert_rejected(db, clock, State.UNKNOWN, State.EXPIRED)
    _assert_rejected(db, clock, State.UNKNOWN, State.REVOKED)


def test_invalid_target_name_rejected(db, clock):
    req_id = make_request(db, State.GRANTED)
    with db.transaction() as s, pytest.raises(InvalidTransition) as exc:
        transition(s, load(s, req_id), "BOGUS", clock)  # type: ignore[arg-type]
    assert exc.value.code == "INVALID_STATE_TRANSITION"


def test_accepts_plain_string_target(db, clock):
    req_id = make_request(db, State.GRANTED)
    with db.transaction() as s:
        transition(s, load(s, req_id), "EXECUTING", clock)  # type: ignore[arg-type]
    assert persisted(db, req_id)[0] == State.EXECUTING.value


def test_full_happy_path(db, clock):
    req_id = make_request(db, State.CREATED)
    path = [State.EVALUATING, State.PENDING_APPROVAL, State.APPROVED, State.GRANTED, State.EXECUTING, State.EXECUTED]
    with db.transaction() as s:
        req = load(s, req_id)
        for dst in path:
            clock.advance(seconds=1)
            transition(s, req, dst, clock)
    assert persisted(db, req_id) == (State.EXECUTED.value, clock())


def test_unknown_reconciliation_path(db, clock):
    req_id = make_request(db, State.EXECUTING)
    with db.transaction() as s:
        req = load(s, req_id)
        for dst in (State.UNKNOWN, State.EXECUTING, State.UNKNOWN, State.FAILED):
            transition(s, req, dst, clock)
    assert persisted(db, req_id)[0] == State.FAILED.value


def test_transition_on_unflushed_request_in_same_transaction(db, clock):
    """Autoflush makes a request added in the same transaction visible to the compare-and-set."""
    with db.transaction() as s:
        s.add(Agent(id="ag_pending", name="a", user_id="u", api_key_hash="p" * 64))
        req = AuthorizationRequest(
            id="req_pending", agent_id="ag_pending", user_id="u", merchant_id="m", order_id="o",
            action_type="purchase", amount_minor=1, currency="USD", request_hash="b" * 64,
            state=State.CREATED.value,  # column defaults apply only at flush; set it explicitly
        )
        s.add(req)
        transition(s, req, State.EVALUATING, clock)
    assert persisted(db, "req_pending") == (State.EVALUATING.value, clock())


def test_failed_transition_rolls_back_with_transaction(db, clock):
    """A successful transition followed by an error in the same transaction is not persisted."""
    req_id = make_request(db, State.GRANTED)
    with pytest.raises(RuntimeError), db.transaction() as s:
        transition(s, load(s, req_id), State.EXECUTING, clock)
        raise RuntimeError("provider call aborted")
    assert persisted(db, req_id) == (State.GRANTED.value, CREATED_AT)


def test_successful_transition_leaves_object_clean(db, clock):
    req_id = make_request(db, State.GRANTED)
    with db.transaction() as s:
        req = load(s, req_id)
        transition(s, req, State.EXECUTING, clock)
        assert req not in s.dirty


# --- Stale objects and concurrency -------------------------------------------------------------


def test_stale_object_raises_concurrent_state_change(db, clock):
    req_id = make_request(db, State.GRANTED)
    with db.transaction() as s:
        stale = load(s, req_id)
    # Another transaction moves the row while we hold the old in-memory object.
    with db.transaction() as s:
        transition(s, load(s, req_id), State.REVOKED, clock)

    clock.advance(minutes=1)
    with db.transaction() as s:
        s.add(stale)  # re-attach the stale (detached) object; its state is still GRANTED in memory
        with pytest.raises(InvalidTransition) as exc:
            transition(s, stale, State.EXECUTING, clock)
    assert exc.value.code == "CONCURRENT_STATE_CHANGE"
    assert persisted(db, req_id)[0] == State.REVOKED.value


def test_stale_object_within_one_session_raises_concurrent_state_change(db, clock):
    """The CAS checks the database row, not the identity map."""
    req_id = make_request(db, State.GRANTED)
    with db.transaction() as s:
        req = load(s, req_id)
        s.execute(
            update(AuthorizationRequest).where(AuthorizationRequest.id == req_id).values(state=State.EXPIRED.value)
            .execution_options(synchronize_session=False)
        )
        assert req.state == State.GRANTED.value
        with pytest.raises(InvalidTransition) as exc:
            transition(s, req, State.EXECUTING, clock)
    assert exc.value.code == "CONCURRENT_STATE_CHANGE"
    assert persisted(db, req_id)[0] == State.EXPIRED.value


def test_missing_row_raises_concurrent_state_change(db, clock):
    req = AuthorizationRequest(id="req_ghost", state=State.GRANTED.value)
    with db.transaction() as s, pytest.raises(InvalidTransition) as exc:
        transition(s, req, State.EXECUTING, clock)
    assert exc.value.code == "CONCURRENT_STATE_CHANGE"


@pytest.mark.parametrize("contenders", [10])
def test_concurrent_transition_exactly_one_wins(db, clock, contenders):
    req_id = make_request(db, State.GRANTED)
    wins: list[int] = []
    codes: list[str] = []
    lock = threading.Lock()

    def worker(i):
        with db.transaction() as s:
            req = load(s, req_id)
            try:
                transition(s, req, State.EXECUTING, clock)
            except InvalidTransition as exc:
                with lock:
                    codes.append(exc.code)
                return
        with lock:
            wins.append(i)

    assert _run_threads(contenders, worker) == []
    assert len(wins) == 1
    assert len(codes) == contenders - 1
    assert set(codes) <= {"CONCURRENT_STATE_CHANGE", "INVALID_STATE_TRANSITION"}
    assert persisted(db, req_id)[0] == State.EXECUTING.value


def test_concurrent_stale_objects_exactly_one_wins(db, clock):
    """Every contender holds the same GRANTED snapshot (loaded before the race), so all losers
    must be caught by the compare-and-set rather than the in-memory check."""
    contenders = 10
    req_id = make_request(db, State.GRANTED)
    with db.transaction() as s:
        template = load(s, req_id)
    wins: list[int] = []
    codes: list[str] = []
    lock = threading.Lock()
    targets = [State.EXECUTING, State.REVOKED, State.EXPIRED]

    def worker(i):
        # Each thread gets its own detached copy of the stale GRANTED snapshot.
        req = AuthorizationRequest(id=template.id, state=template.state)
        with db.transaction() as s:
            try:
                transition(s, req, targets[i % len(targets)], clock)
            except InvalidTransition as exc:
                with lock:
                    codes.append(exc.code)
                return
        with lock:
            wins.append(i)

    assert _run_threads(contenders, worker) == []
    assert len(wins) == 1
    assert codes == ["CONCURRENT_STATE_CHANGE"] * (contenders - 1)
    assert persisted(db, req_id)[0] == targets[wins[0] % len(targets)].value
