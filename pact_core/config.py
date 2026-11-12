"""Runtime configuration. All secrets come from the environment (or .env) and stay server-side."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field("sqlite:///./pact.db", alias="PACT_DATABASE_URL")
    admin_token: SecretStr | None = Field(None, alias="PACT_ADMIN_TOKEN")
    currency: str = Field("USD", alias="PACT_CURRENCY")
    payment_provider: Literal["simulation", "paypal"] = Field("simulation", alias="PACT_PAYMENT_PROVIDER")
    public_url: str = Field("http://localhost:8000", alias="PACT_PUBLIC_URL")
    grant_ttl_seconds: int = Field(600, alias="PACT_GRANT_TTL_SECONDS")
    approval_ttl_seconds: int = Field(1800, alias="PACT_APPROVAL_TTL_SECONDS")
    expiry_sweep_seconds: int = Field(15, alias="PACT_EXPIRY_SWEEP_SECONDS")

    paypal_client_id: str | None = Field(None, alias="PAYPAL_CLIENT_ID")
    paypal_client_secret: SecretStr | None = Field(None, alias="PAYPAL_CLIENT_SECRET")
    paypal_base_url: str = Field("https://api-m.sandbox.paypal.com", alias="PAYPAL_BASE_URL")
    paypal_timeout_seconds: float = Field(15.0, alias="PAYPAL_TIMEOUT_SECONDS")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
