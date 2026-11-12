"""Runtime configuration. All secrets come from the environment (or .env) and stay server-side."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_ADMIN_TOKEN_LENGTH = 16
_PLACEHOLDER_TOKEN_PARTS = ("change-me", "changeme", "placeholder", "your-token", "your_token", "example")
_CURRENCY_RE = re.compile(r"[A-Z]{3}")
# Hosts that can only ever reach a local mock/test server, never real PayPal.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "testserver"})
_LOCAL_SUFFIXES = (".localhost", ".test")


def _is_local_host(host: str) -> bool:
    return host in _LOCAL_HOSTS or host.endswith(_LOCAL_SUFFIXES)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        # `KEY=` in .env means "unset", so an empty credential is treated as missing.
        env_ignore_empty=True,
        # Never echo raw inputs (which may include secrets) in validation error messages.
        hide_input_in_errors=True,
    )

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

    @field_validator("admin_token")
    @classmethod
    def _admin_token_is_strong(cls, value: SecretStr | None) -> SecretStr | None:
        """The admin token approves payments and edits policy: refuse short or placeholder values (SEC-4).
        The message never includes the value."""
        if value is None:
            return value
        token = value.get_secret_value()
        lowered = token.lower()
        if len(token) < MIN_ADMIN_TOKEN_LENGTH or any(p in lowered for p in _PLACEHOLDER_TOKEN_PARTS):
            raise ValueError(
                f"PACT_ADMIN_TOKEN must be at least {MIN_ADMIN_TOKEN_LENGTH} characters and not a placeholder. "
                'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"')
        return value

    @field_validator("currency")
    @classmethod
    def _currency_is_iso_code(cls, value: str) -> str:
        if not _CURRENCY_RE.fullmatch(value):
            raise ValueError("PACT_CURRENCY must be a 3-letter uppercase ISO 4217 code, e.g. USD")
        return value

    @field_validator("paypal_base_url")
    @classmethod
    def _paypal_base_url_is_sandbox(cls, value: str) -> str:
        # PACT never processes real money: only the PayPal Sandbox or a local test server is allowed.
        # The URL itself is not echoed back, in case it embeds credentials.
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        if parts.scheme in ("http", "https") and host and _is_local_host(host):
            return value
        if parts.scheme == "https" and "sandbox" in host.split("."):
            return value
        raise ValueError(
            "PAYPAL_BASE_URL must be an https PayPal Sandbox URL (e.g. https://api-m.sandbox.paypal.com) "
            "or a local test server; live PayPal endpoints are not allowed"
        )

    @model_validator(mode="after")
    def _paypal_requires_credentials(self) -> Settings:
        if self.payment_provider != "paypal":
            return self
        missing = []
        if not (self.paypal_client_id or "").strip():
            missing.append("PAYPAL_CLIENT_ID")
        if self.paypal_client_secret is None or not self.paypal_client_secret.get_secret_value().strip():
            missing.append("PAYPAL_CLIENT_SECRET")
        if missing:
            raise ValueError(
                f"PACT_PAYMENT_PROVIDER=paypal requires {' and '.join(missing)} to be set "
                "(PayPal Sandbox app credentials); set them or use PACT_PAYMENT_PROVIDER=simulation"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
