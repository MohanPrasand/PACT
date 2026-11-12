"""Test environment builder: seeded Merchant Lab + demo policies + agents + simulation adapter + PactCore."""

from __future__ import annotations

from dataclasses import dataclass

from adapters.simulation.adapter import SimulationAdapter
from merchant_lab.seed import seed_catalogue
from merchant_lab.service import MerchantLab, MerchantLabOrderSource
from pact_core.core import PactCore
from pact_core.runtime import AgentIdentity
from pact_core.seed import seed_agents, seed_policies

PUBLIC_URL = "http://localhost:8000"


@dataclass
class PactEnv:
    core: PactCore
    lab: MerchantLab
    sim: SimulationAdapter
    keys: dict[str, str]
    clock: object

    def agent(self, agent_id: str = "ag_shopping") -> AgentIdentity:
        return self.core.authenticate_agent(self.keys[agent_id])

    def order(self, product_id: str, quantity: int = 1, buyer: str = "ag_shopping") -> dict:
        return self.lab.create_order([{"product_id": product_id, "quantity": quantity}], buyer_ref=buyer)

    def product(self, name_fragment: str) -> dict:
        return next(p for p in self.lab.search_products() if name_fragment.lower() in p["name"].lower())

    @staticmethod
    def auth_input(order: dict, **override) -> dict:
        data = {"merchant_id": order["merchant_id"], "order_id": order["order_id"],
                "amount_minor": order["total_minor"], "currency": order["currency"], "description": "test"}
        data.update(override)
        return data

    @staticmethod
    def exec_input(view: dict, order: dict, token: str, **override) -> dict:
        data = {"request_id": view["request_id"], "grant_token": token, "merchant_id": order["merchant_id"],
                "order_id": order["order_id"], "amount_minor": order["total_minor"], "currency": order["currency"]}
        data.update(override)
        return data

    def set_policy(self, policy_id: str, **changes) -> None:
        current = next(p for p in self.core.list_policies() if p["id"] == policy_id)
        self.core.update_policy("test", policy_id, {**current["rules"], **changes}, current["version"])

    def granted(self, product: str = "Atlas", agent_id: str = "ag_shopping") -> tuple[dict, dict, str]:
        """Order + approved + grant token for the given product (approves via admin if needed)."""
        agent = self.agent(agent_id)
        order = self.order(self.product(product)["product_id"], buyer=agent_id)
        view = self.core.request_authorization(agent, self.auth_input(order))
        if view["state"] == "PENDING_APPROVAL":
            self.core.approve("admin", view["request_id"], view["request_hash"])
            view = self.core.get_status(agent, view["request_id"])
        assert view["state"] == "GRANTED", view
        return order, view, view["grant_token"]


def build_env(db, clock) -> PactEnv:
    seed_catalogue(db)
    seed_policies(db)
    keys = seed_agents(db)
    lab = MerchantLab(db)
    sim = SimulationAdapter(PUBLIC_URL)
    core = PactCore(db, MerchantLabOrderSource(lab), {"simulation": sim}, provider="simulation", currency="USD",
                    public_url=PUBLIC_URL, grant_ttl_seconds=600, approval_ttl_seconds=1800, clock=clock)
    return PactEnv(core=core, lab=lab, sim=sim, keys=keys, clock=clock)
