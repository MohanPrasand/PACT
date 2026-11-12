"""Seed PACT policies and demo agents. Used by tests now and by ``pact init`` (#12)."""

from __future__ import annotations

import json
from pathlib import Path

from pact_core.crypto import new_secret, sha256_hex
from pact_core.db import Database
from pact_core.models import Agent, Policy
from pact_core.policy.rules import validate_rules

DEFAULT_POLICY_DIR = Path(__file__).resolve().parents[1] / "specification" / "policies"
DEMO_USER = "u_demo"
DEMO_AGENTS = (
    ("ag_shopping", "Shopping agent", "Buys products for the user"),
    ("ag_research", "Research agent", "Buys datasets and information"),
    ("ag_procurement", "Procurement agent", "Pays approved suppliers"),
)


def _policy_id(stem: str) -> tuple[str, str, str]:
    """File stem -> (policy_id, scope, subject). e.g. 'agent.ag_shopping' -> ('agent:ag_shopping', 'agent', 'ag_shopping')."""
    if stem == "system":
        return "system", "system", "system"
    if stem == "org":
        return "org:default", "org", "default"
    if stem == "user":
        return f"user:{DEMO_USER}", "user", DEMO_USER
    scope, _, subject = stem.partition(".")
    return f"{scope}:{subject}", scope, subject


def seed_policies(db: Database, directory: Path = DEFAULT_POLICY_DIR, *, overwrite: bool = False) -> list[str]:
    """Load every ``*.json`` policy document (validated strictly). Existing policies are kept unless ``overwrite``."""
    created = []
    with db.transaction() as s:
        for path in sorted(Path(directory).glob("*.json")):
            pid, scope, subject = _policy_id(path.stem)
            rules = validate_rules(scope, json.loads(path.read_text()))
            existing = s.get(Policy, pid)
            if existing and not overwrite:
                continue
            if existing:
                existing.rules, existing.version = rules, existing.version + 1
            else:
                s.add(Policy(id=pid, scope=scope, subject=subject, rules=rules, version=1, updated_by="seed"))
            created.append(pid)
    return created


def rotate_agent_key(db: Database, agent_id: str) -> str:
    """Issue a new API key for an existing agent (the old key stops working). Returns the plaintext key once."""
    key = new_secret("pact_ak")
    with db.transaction() as s:
        agent = s.get(Agent, agent_id)
        if agent is None:
            raise KeyError(agent_id)
        agent.api_key_hash = sha256_hex(key)
    return key


def seed_agents(db: Database, agents=DEMO_AGENTS, user_id: str = DEMO_USER) -> dict[str, str]:
    """Create demo agents that don't exist yet. Returns {agent_id: plaintext api key} for new agents only;
    only the key's SHA-256 hash is stored."""
    keys: dict[str, str] = {}
    with db.transaction() as s:
        for agent_id, name, description in agents:
            if s.get(Agent, agent_id):
                continue
            key = new_secret("pact_ak")
            s.add(Agent(id=agent_id, name=name, description=description, user_id=user_id,
                        api_key_hash=sha256_hex(key), active=True))
            keys[agent_id] = key
    return keys
