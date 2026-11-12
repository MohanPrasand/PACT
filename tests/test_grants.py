"""#7 Grant manager: transaction-bound, single-use grants (spec §7.5, §12, §14.3)."""

from __future__ import annotations

import dataclasses
import itertools
import re
import threading
from datetime import timedelta

import pytest
from sqlalchemy import inspect, select, text, update

from pact_core import crypto
from pact_core.errors import Conflict, Forbidden, NotFound, PactError
from pact_core.grants import GrantClaim, GrantManager
from pact_core.models import Agent, AuthorizationRequest, Grant, State

TTL = 600
_ids = itertools.count()


# --- helpers ---------------------------------------------------------------------------------


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


def _binding(n: int, agent_id: str) -> dict:
    return {
        "agent_id": agent_id,
        "user_id": "user_1",
        "merchant_id": "merchant_1",
        "order_id": f"order_{n}",
        "action_type": "purchase",
        "amount_minor": 1999,
        "currency": "USD",
    }


def make_agent(db) -> str:
    n = next(_ids)
    with db.transaction() as s:
        s.add(Agent(id=f"ag_{n}", name="test agent", user_id="user_1", api_key_hash=f"{n:064d}"))
    return f"ag_{n}"


def make_request(db, clock, state: State = State.APPROVED) -> str:
    """Insert an Agent + AuthorizationRequest (with a real binding hash) and return the request id."""
    agent_id = make_agent(db)
    n = next(_ids)
    binding = _binding(n, agent_id)
    with db.transaction() as s:
        s.add(
            AuthorizationRequest(
                id=f"auth_{n}",
                request_hash=crypto.request_hash(**binding),
                state=state.value,
                created_at=clock(),
                updated_at=clock(),
                **binding,
            )
        )
    return f"auth_{n}"


@dataclasses.dataclass
class Issued:
    request_id: str
    grant_id: str
    token: str
    claim: GrantClaim
    request_hash: str


def issue(db, gm: GrantManager, clock, state: State = State.APPROVED) -> Issued:
    req_id = make_request(db, clock, state)
    with db.transaction() as s:
        req = s.get(AuthorizationRequest, req_id)
        grant, token = gm.issue(s, req)
        claim = GrantClaim(
            request_id=req.id,
            token=token,
            agent_id=req.agent_id,
            merchant_id=req.merchant_id,
            order_id=req.order_id,
            amount_minor=req.amount_minor,
            currency=req.currency,
        )
        return Issued(req_id, grant.id, token, claim, req.request_hash)


def validate(db, gm: GrantManager, claim: GrantClaim, current_request_hash: str) -> Grant:
    with db.transaction() as s:
        return gm.validate(s, claim, current_request_hash=current_request_hash)


def expect(code: str, exc_type: type[PactError], fn, *args, **kwargs) -> PactError:
    with pytest.raises(exc_type) as info:
        fn(*args, **kwargs)
    assert info.value.code == code, info.value.code
    return info.value


def consume(db, gm: GrantManager, grant_id: str) -> None:
    with db.transaction() as s:
        gm.consume(s, s.get(Grant, grant_id))


def grant_row(db, grant_id: str) -> Grant:
    with db.transaction() as s:
        g = s.get(Grant, grant_id)
        s.expunge(g)
        return g


@pytest.fixture
def gm(clock) -> GrantManager:
    return GrantManager(clock, TTL)


@pytest.fixture
def issued(db, gm, clock) -> Issued:
    return issue(db, gm, clock)


# --- issue -----------------------------------------------------------------------------------


def test_issue_copies_binding_from_request(db, gm, clock, issued):
    g = grant_row(db, issued.grant_id)
    with db.transaction() as s:
        req = s.get(AuthorizationRequest, issued.request_id)
        for field in ("agent_id", "user_id", "merchant_id", "order_id", "action_type", "amount_minor",
                      "currency", "request_hash"):
            assert getattr(g, field) == getattr(req, field), field
    assert g.request_id == issued.request_id
    assert g.id.startswith("gr_")
    assert g.issued_at == clock()
    assert g.expires_at == clock() + timedelta(seconds=TTL)
    assert g.consumed_at is None and g.revoked_at is None and g.revoked_by is None


def test_issue_does_not_change_request_state(db, issued):
    with db.transaction() as s:
        assert s.get(AuthorizationRequest, issued.request_id).state == State.APPROVED


def test_token_format_and_entropy(issued):
    assert issued.token.startswith("pact_gr_")
    body = issued.token[len("pact_gr_"):]
    assert len(body) >= 43  # 32 random bytes, urlsafe base64
    assert re.fullmatch(r"[A-Za-z0-9_-]+", body)


def test_tokens_and_ids_are_unique(db, gm, clock):
    results = [issue(db, gm, clock) for _ in range(25)]
    assert len({r.token for r in results}) == 25
    assert len({r.grant_id for r in results}) == 25
    with db.transaction() as s:
        assert len(set(s.scalars(select(Grant.token_hash)))) == 25


def test_token_stored_only_as_hash(db, issued):
    g = grant_row(db, issued.grant_id)
    assert g.token_hash == crypto.sha256_hex(issued.token)
    for column in inspect(Grant).columns:
        value = getattr(g, column.key)
        assert value is None or issued.token not in str(value), column.key
    # And nowhere else in the database either.
    with db.engine.connect() as conn:
        for table in inspect(db.engine).get_table_names():
            for row in conn.execute(text(f'SELECT * FROM "{table}"')):
                assert all(issued.token not in str(v) for v in row), table


def test_token_not_in_claim_repr(issued):
    assert issued.token not in repr(issued.claim)
    assert issued.token not in str(issued.claim)


@pytest.mark.parametrize("state", [s for s in State if s is not State.APPROVED])
def test_issue_requires_approved_request(db, gm, clock, state):
    req_id = make_request(db, clock, state)
    with db.transaction() as s:
        expect("GRANT_REQUEST_NOT_APPROVED", Conflict, gm.issue, s, s.get(AuthorizationRequest, req_id))
    with db.transaction() as s:
        assert s.scalar(select(Grant).where(Grant.request_id == req_id)) is None


def test_double_issue_rejected(db, gm, issued):
    with db.transaction() as s:
        expect("GRANT_ALREADY_ISSUED", Conflict, gm.issue, s, s.get(AuthorizationRequest, issued.request_id))
    with db.transaction() as s:
        assert s.scalars(select(Grant).where(Grant.request_id == issued.request_id)).all() == [
            s.get(Grant, issued.grant_id)
        ]


def test_double_issue_rejected_by_unique_constraint(db, gm, issued, monkeypatch):
    """If the pre-check misses a concurrent insert, the unique constraint still yields GRANT_ALREADY_ISSUED."""
    with db.transaction() as s:
        req = s.get(AuthorizationRequest, issued.request_id)
        monkeypatch.setattr(s, "scalar", lambda *a, **k: None)
        expect("GRANT_ALREADY_ISSUED", Conflict, gm.issue, s, req)
        monkeypatch.undo()
        # The outer transaction is still usable after the savepoint rollback.
        assert s.get(Grant, issued.grant_id) is not None


def test_ttl_must_be_positive(clock):
    with pytest.raises(ValueError):
        GrantManager(clock, 0)


# --- validate: happy path ----------------------------------------------------------------------


def test_valid_grant_validates_and_consumes(db, gm, clock, issued):
    g = validate(db, gm, issued.claim, issued.request_hash)
    assert g.id == issued.grant_id
    consume(db, gm, issued.grant_id)
    assert grant_row(db, issued.grant_id).consumed_at == clock()


def test_validate_does_not_mutate(db, gm, issued):
    before = {c.key: getattr(grant_row(db, issued.grant_id), c.key) for c in inspect(Grant).columns}
    validate(db, gm, issued.claim, issued.request_hash)
    after = {c.key: getattr(grant_row(db, issued.grant_id), c.key) for c in inspect(Grant).columns}
    assert before == after


def test_currency_compared_case_insensitively(db, gm, issued):
    claim = dataclasses.replace(issued.claim, currency="usd")
    assert validate(db, gm, claim, issued.request_hash).id == issued.grant_id


def test_valid_until_just_before_expiry(db, gm, clock, issued):
    clock.advance(seconds=TTL - 1)
    validate(db, gm, issued.claim, issued.request_hash)
    consume(db, gm, issued.grant_id)


# --- validate: each error code -----------------------------------------------------------------


def test_unknown_request_not_found(db, gm, issued):
    claim = dataclasses.replace(issued.claim, request_id="auth_nope")
    expect("GRANT_NOT_FOUND", NotFound, validate, db, gm, claim, issued.request_hash)


def test_request_without_grant_not_found(db, gm, clock):
    req_id = make_request(db, clock)
    claim = GrantClaim(req_id, "pact_gr_x", "ag_x", "merchant_1", "order_x", 1999, "USD")
    expect("GRANT_NOT_FOUND", NotFound, validate, db, gm, claim, "0" * 64)


@pytest.mark.parametrize("token", ["", "pact_gr_wrong", "TOKEN_UPPER"])
def test_wrong_token_invalid(db, gm, issued, token):
    if token == "TOKEN_UPPER":
        token = issued.token.upper()
    claim = dataclasses.replace(issued.claim, token=token)
    expect("GRANT_INVALID", Forbidden, validate, db, gm, claim, issued.request_hash)


def test_token_of_another_grant_invalid(db, gm, clock, issued):
    other = issue(db, gm, clock)
    claim = dataclasses.replace(issued.claim, token=other.token)
    expect("GRANT_INVALID", Forbidden, validate, db, gm, claim, issued.request_hash)


def test_token_compared_in_constant_time(db, gm, issued, monkeypatch):
    calls = []
    real = crypto.constant_time_equal

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(crypto, "constant_time_equal", spy)
    validate(db, gm, issued.claim, issued.request_hash)
    assert (crypto.sha256_hex(issued.token), grant_row(db, issued.grant_id).token_hash) in calls


def test_different_agent_rejected(db, gm, issued):
    other_agent = make_agent(db)
    claim = dataclasses.replace(issued.claim, agent_id=other_agent)
    expect("GRANT_AGENT_MISMATCH", Forbidden, validate, db, gm, claim, issued.request_hash)


def test_revoked_grant_rejected(db, gm, issued):
    with db.transaction() as s:
        assert gm.revoke(s, issued.request_id, "admin") is True
    expect("GRANT_REVOKED", Conflict, validate, db, gm, issued.claim, issued.request_hash)
    expect("GRANT_REVOKED", Conflict, consume, db, gm, issued.grant_id)


def test_replay_rejected(db, gm, issued):
    validate(db, gm, issued.claim, issued.request_hash)
    consume(db, gm, issued.grant_id)
    expect("GRANT_ALREADY_USED", Conflict, validate, db, gm, issued.claim, issued.request_hash)
    expect("GRANT_ALREADY_USED", Conflict, consume, db, gm, issued.grant_id)


@pytest.mark.parametrize("delta", [timedelta(seconds=TTL), timedelta(seconds=TTL + 1), timedelta(days=1)])
def test_expired_grant_rejected(db, gm, clock, issued, delta):
    clock.advance(seconds=delta.total_seconds())
    expect("GRANT_EXPIRED", Conflict, validate, db, gm, issued.claim, issued.request_hash)
    expect("GRANT_EXPIRED", Conflict, consume, db, gm, issued.grant_id)
    assert grant_row(db, issued.grant_id).consumed_at is None


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"merchant_id": "merchant_evil"}, "merchant"),
        ({"order_id": "order_other"}, "order"),
        ({"amount_minor": 2000}, "amount"),
        ({"amount_minor": 1998}, "amount"),
        ({"amount_minor": 0}, "amount"),
        ({"amount_minor": "1999"}, "amount"),
        ({"amount_minor": 1999.0}, "amount"),
        ({"amount_minor": True}, "amount"),
        ({"currency": "EUR"}, "currency"),
        ({"currency": "US"}, "currency"),
        ({"currency": ""}, "currency"),
    ],
)
def test_binding_mismatch(db, gm, issued, change, field):
    claim = dataclasses.replace(issued.claim, **change)
    err = expect("GRANT_BINDING_MISMATCH", Conflict, validate, db, gm, claim, issued.request_hash)
    assert err.details == {"field": field}


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"merchant_id": "m", "order_id": "o", "amount_minor": 1, "currency": "EUR"}, "merchant"),
        ({"order_id": "o", "amount_minor": 1, "currency": "EUR"}, "order"),
        ({"amount_minor": 1, "currency": "EUR"}, "amount"),
    ],
)
def test_binding_mismatch_reports_first_field(db, gm, issued, change, field):
    claim = dataclasses.replace(issued.claim, **change)
    err = expect("GRANT_BINDING_MISMATCH", Conflict, validate, db, gm, claim, issued.request_hash)
    assert err.details == {"field": field}


def test_tampered_request_row_hash_rejected(db, gm, issued):
    with db.transaction() as s:
        s.execute(
            update(AuthorizationRequest)
            .where(AuthorizationRequest.id == issued.request_id)
            .values(request_hash="f" * 64)
        )
    expect("REQUEST_HASH_MISMATCH", Conflict, validate, db, gm, issued.claim, issued.request_hash)


def test_tampered_grant_hash_rejected(db, gm, issued):
    with db.transaction() as s:
        s.execute(update(Grant).where(Grant.id == issued.grant_id).values(request_hash="e" * 64))
    expect("REQUEST_HASH_MISMATCH", Conflict, validate, db, gm, issued.claim, issued.request_hash)


def test_order_modified_after_approval(db, gm, issued):
    """The caller recomputes the hash from the live order; a changed total yields a different hash."""
    with db.transaction() as s:
        g = s.get(Grant, issued.grant_id)
        live = crypto.request_hash(
            agent_id=g.agent_id, user_id=g.user_id, merchant_id=g.merchant_id, order_id=g.order_id,
            action_type=g.action_type, amount_minor=g.amount_minor + 500, currency=g.currency,
        )
    assert live != issued.request_hash
    expect("ORDER_MODIFIED", Conflict, validate, db, gm, issued.claim, live)


def test_current_hash_currency_case_does_not_trigger_order_modified(db, gm, issued):
    with db.transaction() as s:
        g = s.get(Grant, issued.grant_id)
        live = crypto.request_hash(
            agent_id=g.agent_id, user_id=g.user_id, merchant_id=g.merchant_id, order_id=g.order_id,
            action_type=g.action_type, amount_minor=g.amount_minor, currency=g.currency.lower(),
        )
    validate(db, gm, issued.claim, live)


# --- validate: check-order precedence ----------------------------------------------------------


def _all_wrong(db, issued) -> GrantClaim:
    return dataclasses.replace(
        issued.claim, token="pact_gr_wrong", agent_id=make_agent(db), merchant_id="m", currency="EUR"
    )


def test_precedence_full_chain(db, gm, clock, issued):
    """Peel off one failure at a time: each step's code wins over every later one."""
    with db.transaction() as s:  # make every condition fail at once
        s.execute(update(AuthorizationRequest).where(AuthorizationRequest.id == issued.request_id)
                  .values(request_hash="f" * 64))
        assert gm.revoke(s, issued.request_id, "admin")
    clock.advance(seconds=TTL + 1)
    claim = _all_wrong(db, issued)
    stale = "d" * 64

    expect("GRANT_NOT_FOUND", NotFound, validate, db, gm, dataclasses.replace(claim, request_id="nope"), stale)
    expect("GRANT_INVALID", Forbidden, validate, db, gm, claim, stale)
    claim = dataclasses.replace(claim, token=issued.token)
    expect("GRANT_AGENT_MISMATCH", Forbidden, validate, db, gm, claim, stale)
    claim = dataclasses.replace(claim, agent_id=issued.claim.agent_id)
    expect("GRANT_REVOKED", Conflict, validate, db, gm, claim, stale)
    with db.transaction() as s:  # un-revoke and mark consumed (only reachable by tampering)
        s.execute(update(Grant).where(Grant.id == issued.grant_id).values(revoked_at=None, consumed_at=clock()))
    expect("GRANT_ALREADY_USED", Conflict, validate, db, gm, claim, stale)
    with db.transaction() as s:
        s.execute(update(Grant).where(Grant.id == issued.grant_id).values(consumed_at=None))
    expect("GRANT_EXPIRED", Conflict, validate, db, gm, claim, stale)
    clock.advance(seconds=-(TTL + 1))
    err = expect("GRANT_BINDING_MISMATCH", Conflict, validate, db, gm, claim, stale)
    assert err.details == {"field": "merchant"}
    claim = issued.claim
    expect("REQUEST_HASH_MISMATCH", Conflict, validate, db, gm, claim, stale)
    with db.transaction() as s:
        s.execute(update(AuthorizationRequest).where(AuthorizationRequest.id == issued.request_id)
                  .values(request_hash=issued.request_hash))
    expect("ORDER_MODIFIED", Conflict, validate, db, gm, claim, stale)
    validate(db, gm, claim, issued.request_hash)


def test_wrong_agent_and_expired_reports_agent(db, gm, clock, issued):
    clock.advance(seconds=TTL + 1)
    claim = dataclasses.replace(issued.claim, agent_id=make_agent(db))
    expect("GRANT_AGENT_MISMATCH", Forbidden, validate, db, gm, claim, issued.request_hash)


def test_wrong_token_and_wrong_agent_reports_token(db, gm, issued):
    claim = dataclasses.replace(issued.claim, token="pact_gr_x", agent_id=make_agent(db))
    expect("GRANT_INVALID", Forbidden, validate, db, gm, claim, issued.request_hash)


def test_consumed_and_expired_reports_already_used(db, gm, clock, issued):
    consume(db, gm, issued.grant_id)
    clock.advance(seconds=TTL + 1)
    expect("GRANT_ALREADY_USED", Conflict, validate, db, gm, issued.claim, issued.request_hash)


def test_expired_and_binding_mismatch_reports_expired(db, gm, clock, issued):
    clock.advance(seconds=TTL)
    claim = dataclasses.replace(issued.claim, amount_minor=1)
    expect("GRANT_EXPIRED", Conflict, validate, db, gm, claim, issued.request_hash)


def test_binding_mismatch_before_order_modified(db, gm, issued):
    claim = dataclasses.replace(issued.claim, order_id="order_other")
    err = expect("GRANT_BINDING_MISMATCH", Conflict, validate, db, gm, claim, "c" * 64)
    assert err.details == {"field": "order"}


# --- consume -----------------------------------------------------------------------------------


def test_consume_keeps_orm_object_consistent(db, gm, clock, issued):
    with db.transaction() as s:
        g = s.get(Grant, issued.grant_id)
        gm.consume(s, g)
        assert g.consumed_at == clock()
        assert g not in s.dirty
    assert grant_row(db, issued.grant_id).consumed_at == clock()


def test_consume_with_stale_object_rejected(db, gm, issued):
    stale = grant_row(db, issued.grant_id)  # loaded before another transaction consumes it
    consume(db, gm, issued.grant_id)
    with db.transaction() as s:
        expect("GRANT_ALREADY_USED", Conflict, gm.consume, s, s.merge(stale, load=False))


def test_consume_rolled_back_with_transaction(db, gm, issued):
    with pytest.raises(RuntimeError):
        with db.transaction() as s:
            gm.consume(s, s.get(Grant, issued.grant_id))
            raise RuntimeError("provider call never happened")
    assert grant_row(db, issued.grant_id).consumed_at is None
    consume(db, gm, issued.grant_id)


def test_consume_missing_grant_not_found(db, gm, issued):
    ghost = Grant(id="gr_ghost")
    with db.transaction() as s:
        expect("GRANT_NOT_FOUND", NotFound, gm.consume, s, ghost)


def test_concurrent_consume_exactly_one_wins(db, gm, issued):
    contenders = 10
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(_i):
        with db.transaction() as s:
            g = s.get(Grant, issued.grant_id)
            try:
                gm.consume(s, g)
                result = "ok"
            except PactError as exc:
                result = exc.code
        with lock:
            outcomes.append(result)

    assert _run_threads(contenders, worker) == []
    assert sorted(outcomes) == ["GRANT_ALREADY_USED"] * (contenders - 1) + ["ok"]
    assert grant_row(db, issued.grant_id).consumed_at is not None


def test_concurrent_validate_then_consume_exactly_one_wins(db, gm, issued):
    """Both executors can pass validation on a snapshot; only one consume succeeds."""
    contenders = 10
    outcomes: list[str] = []
    lock = threading.Lock()
    snapshots = []
    for _ in range(contenders):
        snapshots.append(validate(db, gm, issued.claim, issued.request_hash))

    def worker(i):
        with db.transaction() as s:
            try:
                gm.consume(s, s.merge(snapshots[i], load=False))
                result = "ok"
            except PactError as exc:
                result = exc.code
        with lock:
            outcomes.append(result)

    assert _run_threads(contenders, worker) == []
    assert outcomes.count("ok") == 1
    assert outcomes.count("GRANT_ALREADY_USED") == contenders - 1


# --- revoke ------------------------------------------------------------------------------------


def test_revoke_unused_grant(db, gm, clock, issued):
    with db.transaction() as s:
        assert gm.revoke(s, issued.request_id, "admin:alice") is True
    g = grant_row(db, issued.grant_id)
    assert g.revoked_at == clock()
    assert g.revoked_by == "admin:alice"
    assert g.consumed_at is None


def test_revoke_twice_returns_false(db, gm, clock, issued):
    with db.transaction() as s:
        assert gm.revoke(s, issued.request_id, "admin:alice") is True
    revoked_at = grant_row(db, issued.grant_id).revoked_at
    clock.advance(seconds=5)
    with db.transaction() as s:
        assert gm.revoke(s, issued.request_id, "admin:bob") is False
    g = grant_row(db, issued.grant_id)
    assert (g.revoked_at, g.revoked_by) == (revoked_at, "admin:alice")


def test_consumed_grant_cannot_be_revoked(db, gm, issued):
    consume(db, gm, issued.grant_id)
    with db.transaction() as s:
        assert gm.revoke(s, issued.request_id, "admin") is False
    g = grant_row(db, issued.grant_id)
    assert g.revoked_at is None and g.consumed_at is not None


def test_revoke_without_grant_returns_false(db, gm, clock):
    req_id = make_request(db, clock)
    with db.transaction() as s:
        assert gm.revoke(s, req_id, "admin") is False
        assert gm.revoke(s, "auth_unknown", "admin") is False


def test_revoke_updates_loaded_object(db, gm, clock, issued):
    with db.transaction() as s:
        g = s.get(Grant, issued.grant_id)
        assert gm.revoke(s, issued.request_id, "admin")
        assert g.revoked_at == clock() and g.revoked_by == "admin"
        assert g not in s.dirty


def test_revoke_then_consume_rejected(db, gm, issued):
    with db.transaction() as s:
        g = s.get(Grant, issued.grant_id)
        gm.revoke(s, issued.request_id, "admin")
        expect("GRANT_REVOKED", Conflict, gm.consume, s, g)


def test_revoke_does_not_change_request_state(db, gm, issued):
    with db.transaction() as s:
        gm.revoke(s, issued.request_id, "admin")
    with db.transaction() as s:
        assert s.get(AuthorizationRequest, issued.request_id).state == State.APPROVED


# --- find_expired_unconsumed -------------------------------------------------------------------


def test_find_expired_unconsumed(db, gm, clock):
    fresh = issue(db, gm, clock)
    used = issue(db, gm, clock)
    revoked = issue(db, gm, clock)
    stale = issue(db, gm, clock)
    consume(db, gm, used.grant_id)
    with db.transaction() as s:
        gm.revoke(s, revoked.request_id, "admin")
    clock.advance(seconds=10)
    later = issue(db, gm, clock)  # expires 10 s after the others

    with db.transaction() as s:
        assert gm.find_expired_unconsumed(s) == []

    clock.advance(seconds=TTL - 10)  # exactly at the first batch's expires_at
    with db.transaction() as s:
        found = {g.id for g in gm.find_expired_unconsumed(s)}
    assert found == {fresh.grant_id, stale.grant_id}

    clock.advance(seconds=10)
    with db.transaction() as s:
        found = [g.id for g in gm.find_expired_unconsumed(s)]
    assert set(found) == {fresh.grant_id, stale.grant_id, later.grant_id}
    assert found[-1] == later.grant_id  # oldest expiry first
