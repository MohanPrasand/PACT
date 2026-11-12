# Phase 1 — Core Foundation: Parallel Implementation Plan

Tracking: epic #18 · Issues: #1, #2, #3, #4, #5 · Spec: `specification/PROJECT_SPEC.md` §5, §7.2, §7.4, §12, §14.1, §15

## Goal
Merge a tested Phase 1 core into `dev`: configuration and CI (#1), persistence and concurrency model (#2), authorization state machine (#3), deterministic policy engine (#4), tamper-evident audit log (#5).

Starting point: this branch already contains first-draft code for #2–#5 (imported from `feat/core-foundation`). Agents **review, complete and test** that code against each issue's acceptance criteria. They do not rewrite it from scratch.

## Branch model

```
main
 └─ dev
     └─ feat/phase-1-core-foundation          ← integration branch (this plan)
         ├─ feat/phase-1/1-setup-ci           ← agent A  (PR → integration)
         ├─ feat/phase-1/3-state-machine      ← agent B  (PR → integration)
         ├─ feat/phase-1/4-policy-engine      ← agent C  (PR → integration)
         └─ feat/phase-1/5-audit-log          ← agent D  (PR → integration)
```

#2 is done directly on the integration branch before the agents start, because #3 and #5 build on it.
When Phase 1 is complete: one PR `feat/phase-1-core-foundation → dev`.
GitHub auto-closes issues only on merges into the default branch (`main`), so issues are closed manually after the `dev` merge.

## Dependency graph

```
#2 persistence ──┬──> #3 state machine
                 ├──> #5 audit log
                 └──> (#4 policy engine: nominal only; it is a pure function with no DB)
#1 setup/CI      (independent)
```

## Waves

### Wave 0 (sequential, main session) — #2 Persistence
Must merge before Wave 1 starts.
- [ ] `tests/conftest.py` with shared fixtures (Wave 1 agents may **use** them but must **not edit** them):
  - `db` — temp-file SQLite `Database` with `create_all()` (a file, not `:memory:`, so threads share it)
  - `clock` — controllable `FixedClock` (`.now`, `.advance(seconds=…)`)
- [ ] `tests/test_db.py`: tables created; `BEGIN IMMEDIATE` serialises writers (threaded conditional-UPDATE test, no lost updates); transaction rollback on exception
- [ ] Freeze `pact_core/models.py` for Wave 1 (any needed field changes are made here first)
- [ ] Commit to the integration branch and push

### Wave 1 (parallel, 4 agents, each in its own git worktree)

| Agent | Issue | Branch | Owns (may edit) | Must not edit |
|---|---|---|---|---|
| A | #1 Setup, config, CI | `feat/phase-1/1-setup-ci` | `pyproject.toml`, `uv.lock`, `pact_core/config.py`, `.github/workflows/ci.yml`, `README.md` (dev section), `tests/test_config.py` | everything else |
| B | #3 State machine | `feat/phase-1/3-state-machine` | `pact_core/state_machine.py`, `tests/test_state_machine.py` | `models.py`, `db.py`, `conftest.py` |
| C | #4 Policy engine | `feat/phase-1/4-policy-engine` | `pact_core/policy/**`, `specification/policies/*.json` (examples), `tests/test_policy_engine.py` | `models.py`, `db.py`, `conftest.py` |
| D | #5 Audit log | `feat/phase-1/5-audit-log` | `pact_core/audit.py`, `tests/test_audit.py` | `models.py`, `db.py`, `conftest.py` |

Disjoint file ownership means the four PRs merge without conflicts. If an agent needs a change outside its files (e.g. a model field), it **stops and reports** instead of editing.

#### Per-agent definition of done
1. Every acceptance-criteria checkbox in the issue (`gh issue view <n> -R MohanPrasand/PACT`) is satisfied or explicitly reported as not satisfiable.
2. `uv run pytest` passes on the agent's branch (the whole suite, not just its own tests).
3. Commits follow `type(scope): subject` with `Refs #<n>`, ending with the repo's `Co-Authored-By` trailer.
4. Branch pushed; PR opened **into `feat/phase-1-core-foundation`** titled `[P1] #<n> <title>`, with a body listing each acceptance criterion and how it's covered.
5. Final report: PR URL, test count, deviations, open questions.

#### Agent-specific notes
- **A (#1):** `Settings` must fail fast with a clear error when `PACT_PAYMENT_PROVIDER=paypal` and credentials are missing (model validator), and must never render secrets in `repr`/errors (`SecretStr`). CI: `astral-sh/setup-uv`, `uv sync`, `uv run pytest`, triggered on push/PR to `dev`, `main` and `feat/**`. CI must not require PayPal secrets.
- **B (#3):** table-driven test over the full transition matrix (every allowed transition succeeds; every forbidden one raises `INVALID_STATE_TRANSITION`); terminal states can't be left; concurrency test where N threads attempt the same transition from the same source state and exactly one wins (`CONCURRENT_STATE_CHANGE` for the rest).
- **C (#4):** pure-function tests (no DB) building `EvaluationInput` / `PolicySnapshot` directly. Cover the spec §14.1 matrix plus: conflicting policies (approval + deny → DENY), merchant category/agent restrictions, duplicate window effect `DENY` vs `REQUIRE_APPROVAL`, ORDER_MISMATCH for each field, missing policy doc → fail closed, exception → `POLICY_EVALUATION_ERROR`, determinism (same input → identical output and `policy_version`), `policy_version` changes when any doc changes. Add example policy JSON under `specification/policies/`.
- **D (#5):** tests for chain validity; editing an event's `data`, `event_type` or `ts` → `verify_chain` returns `valid=False` with `broken_at_seq`; deleting a middle event and deleting the last event are both detected; concurrent appends from threads produce a single unforked chain with contiguous `seq`; the audit write rolls back with its enclosing transaction.

### Wave 2 (sequential, main session) — Integrate
- [ ] Review each PR diff (scope respected, no secrets, tests meaningful)
- [ ] Merge into the integration branch in order **#1 → #4 → #3 → #5** (CI first, so later merges run under CI)
- [ ] Run the full suite on the integration branch; fix any cross-PR interaction
- [ ] Open PR `feat/phase-1-core-foundation → dev` listing #1–#5; after merge, close the issues and tick them in epic #18

## Shared engineering rules (all agents)
- Amounts are integer minor units; timestamps are naive UTC from the injected `Clock`.
- One `Database.transaction()` per write path, no nesting, no network I/O inside a transaction.
- Atomicity comes from conditional `UPDATE … WHERE` + row-count checks (portable to PostgreSQL).
- Policy decisions are deterministic; no LLM, network or wall-clock reads inside `evaluate`.
- Never print, log or commit secrets; `.env` stays gitignored.
- Scope guard (spec §4.3, §18): no features beyond the issue.

## Risks
| Risk | Mitigation |
|---|---|
| Agents edit shared files and conflict | Ownership table + "stop and report" rule; `conftest.py`/`models.py` frozen after Wave 0 |
| SQLite thread tests flaky | Temp-file DB, `busy_timeout`, generous thread joins; no `:memory:` across threads |
| Agent rewrites WIP instead of completing it | Brief says review/complete; Wave 2 review checks the diff size |
| Hidden dependency on Phase 2+ code | Integration branch contains only Phase 1 modules; `pact_core` must not import `merchant_lab`/`adapters` |

## Estimated cost
Wave 0 and Wave 2 run in the main session. Wave 1 runs 4 parallel agents.
