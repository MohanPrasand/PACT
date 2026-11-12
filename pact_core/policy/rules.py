"""Typed policy documents. Each scope has its own schema; unknown fields are rejected so a typo in
a policy cannot silently disable a restriction. Validation is strict (no "5000" -> 5000 or
true -> 1 coercion) and limits are integer minor units that may not be negative."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Rules(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SystemPolicy(_Rules):
    """Hard restrictions. Human approval can never override these."""

    currency: str = Field("USD", pattern=r"^[A-Z]{3}$")
    hard_max_transaction_minor: int = Field(100_000, ge=1)
    allowed_actions: list[str] = ["PURCHASE"]


class OrgPolicy(_Rules):
    daily_budget_minor: int = Field(..., ge=0)
    approved_merchants: list[str] = []
    max_transaction_minor: int | None = Field(None, ge=1)
    duplicate_window_minutes: int = Field(10, ge=0)
    duplicate_effect: Literal["DENY", "REQUIRE_APPROVAL"] = "REQUIRE_APPROVAL"


class UserPolicy(_Rules):
    daily_limit_minor: int = Field(..., ge=0)
    approval_threshold_minor: int = Field(..., ge=0)
    max_transaction_minor: int | None = Field(None, ge=1)


class MerchantPolicy(_Rules):
    max_transaction_minor: int | None = Field(None, ge=1)
    allowed_categories: list[str] | None = None
    allowed_agents: list[str] | None = None
    approval_required_above_minor: int | None = Field(None, ge=0)


class AgentPolicy(_Rules):
    enabled: bool = True
    max_transaction_minor: int = Field(..., ge=0)
    daily_limit_minor: int = Field(..., ge=0)
    allowed_merchants: list[str] | None = None
    approval_required_above_minor: int | None = Field(None, ge=0)


SCHEMAS: dict[str, type[_Rules]] = {
    "system": SystemPolicy,
    "org": OrgPolicy,
    "user": UserPolicy,
    "merchant": MerchantPolicy,
    "agent": AgentPolicy,
}


def validate_rules(scope: str, rules: dict) -> dict:
    schema = SCHEMAS.get(scope)
    if schema is None:
        raise ValueError(f"Unknown policy scope: {scope}")
    return schema.model_validate(rules).model_dump()
