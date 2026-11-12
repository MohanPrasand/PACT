"""Settings validation tests.

Isolation: every test runs with all PACT_*/PAYPAL_* variables removed from the environment and with the
working directory moved to a temp dir, and settings are built with ``_env_file=None`` (or an explicit
temp file). The developer's real ``.env`` is therefore never read.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from pact_core.config import Settings, get_settings

REPO_ROOT = Path(__file__).resolve().parent.parent

ADMIN_TOKEN = "admin-token-do-not-leak-7f3a"
CLIENT_ID = "sandbox-client-id-123"
CLIENT_SECRET = "client-secret-do-not-leak-9c1e"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith(("PACT_", "PAYPAL_")):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def load(monkeypatch, **env: str) -> Settings:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def error_text(exc: pytest.ExceptionInfo[ValidationError]) -> str:
    return str(exc.value) + repr(exc.value)


# --- defaults and loading -------------------------------------------------------------------------


def test_defaults_are_safe(monkeypatch):
    s = load(monkeypatch)
    assert s.payment_provider == "simulation"
    assert s.currency == "USD"
    assert s.paypal_base_url == "https://api-m.sandbox.paypal.com"
    assert s.admin_token is None
    assert s.paypal_client_secret is None
    assert s.paypal_landing_page == "LOGIN"


def test_values_come_from_environment(monkeypatch):
    s = load(
        monkeypatch,
        PACT_CURRENCY="INR",
        PACT_GRANT_TTL_SECONDS="120",
        PACT_ADMIN_TOKEN=ADMIN_TOKEN,
        PAYPAL_TIMEOUT_SECONDS="2.5",
    )
    assert s.currency == "INR"
    assert s.grant_ttl_seconds == 120
    assert s.paypal_timeout_seconds == 2.5
    assert isinstance(s.admin_token, SecretStr)
    assert s.admin_token.get_secret_value() == ADMIN_TOKEN


def test_values_from_constructor_kwargs():
    s = Settings(_env_file=None, PACT_CURRENCY="EUR", PACT_APPROVAL_TTL_SECONDS=60)  # type: ignore[call-arg]
    assert s.currency == "EUR"
    assert s.approval_ttl_seconds == 60


def test_dotenv_in_working_directory_is_not_read_when_disabled(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("PACT_CURRENCY=EUR\n")
    assert load(monkeypatch).currency == "USD"


def test_explicit_env_file_is_read(tmp_path):
    env_file = tmp_path / "custom.env"
    env_file.write_text("PACT_CURRENCY=GBP\nPAYPAL_CLIENT_ID=\n")
    s = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert s.currency == "GBP"
    assert s.paypal_client_id is None  # empty value means "unset"


def test_env_example_template_loads(tmp_path):
    # The committed template (no secrets) must produce valid settings as-is.
    s = Settings(_env_file=REPO_ROOT / ".env.example")  # type: ignore[call-arg]
    assert s.payment_provider == "simulation"
    assert s.paypal_client_id is None
    assert s.paypal_client_secret is None
    assert s.paypal_landing_page == "LOGIN"


def test_get_settings_is_cached():
    assert get_settings() is get_settings()


# --- PayPal provider requires credentials ---------------------------------------------------------


def test_paypal_without_credentials_fails_fast(monkeypatch):
    with pytest.raises(ValidationError) as exc:
        load(monkeypatch, PACT_PAYMENT_PROVIDER="paypal")
    text = str(exc.value)
    assert "PACT_PAYMENT_PROVIDER=paypal requires PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET" in text


@pytest.mark.parametrize(
    ("env", "missing"),
    [
        ({"PAYPAL_CLIENT_SECRET": CLIENT_SECRET}, "PAYPAL_CLIENT_ID"),
        ({"PAYPAL_CLIENT_ID": CLIENT_ID}, "PAYPAL_CLIENT_SECRET"),
        ({"PAYPAL_CLIENT_ID": "", "PAYPAL_CLIENT_SECRET": CLIENT_SECRET}, "PAYPAL_CLIENT_ID"),
        ({"PAYPAL_CLIENT_ID": CLIENT_ID, "PAYPAL_CLIENT_SECRET": ""}, "PAYPAL_CLIENT_SECRET"),
        ({"PAYPAL_CLIENT_ID": "   ", "PAYPAL_CLIENT_SECRET": CLIENT_SECRET}, "PAYPAL_CLIENT_ID"),
        ({"PAYPAL_CLIENT_ID": CLIENT_ID, "PAYPAL_CLIENT_SECRET": "  "}, "PAYPAL_CLIENT_SECRET"),
    ],
)
def test_paypal_with_one_credential_missing_names_it(monkeypatch, env, missing):
    with pytest.raises(ValidationError) as exc:
        load(monkeypatch, PACT_PAYMENT_PROVIDER="paypal", **env)
    text = str(exc.value)
    assert f"PACT_PAYMENT_PROVIDER=paypal requires {missing} to be set" in text
    assert CLIENT_SECRET not in error_text(exc)


def test_paypal_with_credentials_loads(monkeypatch):
    s = load(
        monkeypatch,
        PACT_PAYMENT_PROVIDER="paypal",
        PAYPAL_CLIENT_ID=CLIENT_ID,
        PAYPAL_CLIENT_SECRET=CLIENT_SECRET,
    )
    assert s.payment_provider == "paypal"
    assert s.paypal_client_id == CLIENT_ID
    assert isinstance(s.paypal_client_secret, SecretStr)
    assert s.paypal_client_secret.get_secret_value() == CLIENT_SECRET


def test_simulation_does_not_need_credentials(monkeypatch):
    assert load(monkeypatch, PACT_PAYMENT_PROVIDER="simulation").paypal_client_secret is None


def test_unknown_provider_rejected(monkeypatch):
    with pytest.raises(ValidationError):
        load(monkeypatch, PACT_PAYMENT_PROVIDER="stripe")


# --- currency -------------------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["USD", "INR", "EUR"])
def test_valid_currency(monkeypatch, code):
    assert load(monkeypatch, PACT_CURRENCY=code).currency == code


@pytest.mark.parametrize("code", ["usd", "Usd", "US", "USDX", "U5D", " USD", "USD\n"])
def test_invalid_currency_rejected(monkeypatch, code):
    with pytest.raises(ValidationError) as exc:
        load(monkeypatch, PACT_CURRENCY=code)
    assert "PACT_CURRENCY must be a 3-letter uppercase ISO 4217 code" in str(exc.value)


def test_empty_currency_kwarg_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, PACT_CURRENCY="")  # type: ignore[call-arg]


# --- PayPal landing page --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["LOGIN", "GUEST_CHECKOUT", "NO_PREFERENCE"])
def test_valid_landing_page(monkeypatch, value):
    assert load(monkeypatch, PAYPAL_LANDING_PAGE=value).paypal_landing_page == value


@pytest.mark.parametrize("value", ["login", "BILLING", "GUEST", "LOGIN ", "0"])
def test_invalid_landing_page_rejected(monkeypatch, value):
    with pytest.raises(ValidationError) as exc:
        load(monkeypatch, PAYPAL_LANDING_PAGE=value)
    assert "PAYPAL_LANDING_PAGE" in str(exc.value)


def test_empty_landing_page_means_default(monkeypatch):
    assert load(monkeypatch, PAYPAL_LANDING_PAGE="").paypal_landing_page == "LOGIN"


# --- PayPal base URL: Sandbox or local only (no real money) ---------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://api-m.sandbox.paypal.com",
        "https://api.sandbox.paypal.com/",
        "https://API-M.SANDBOX.PAYPAL.COM",
        "http://localhost:8080",
        "https://localhost",
        "http://127.0.0.1:9000/v2",
        "http://[::1]:9000",
        "http://testserver",
        "http://paypal-mock.test",
    ],
)
def test_sandbox_or_local_base_url_accepted(monkeypatch, url):
    assert load(monkeypatch, PAYPAL_BASE_URL=url).paypal_base_url == url


@pytest.mark.parametrize(
    "url",
    [
        "https://api-m.paypal.com",  # live
        "https://api.paypal.com",  # live
        "http://api-m.sandbox.paypal.com",  # sandbox over plain http
        "https://notsandbox.example.com",
        "https://sandbox-paypal.example.com",
        "https://example.com/sandbox",  # "sandbox" in path, not host
        "ftp://localhost",
        "localhost:8080",
        "not a url",
    ],
)
def test_live_or_unknown_base_url_rejected(monkeypatch, url):
    with pytest.raises(ValidationError) as exc:
        load(monkeypatch, PAYPAL_BASE_URL=url)
    assert "PAYPAL_BASE_URL must be an https PayPal Sandbox URL" in str(exc.value)


def test_base_url_is_checked_even_in_simulation_mode(monkeypatch):
    with pytest.raises(ValidationError):
        load(monkeypatch, PACT_PAYMENT_PROVIDER="simulation", PAYPAL_BASE_URL="https://api-m.paypal.com")


# --- secrets never rendered -----------------------------------------------------------------------


def test_secrets_not_in_repr_or_str(monkeypatch):
    s = load(
        monkeypatch,
        PACT_PAYMENT_PROVIDER="paypal",
        PACT_ADMIN_TOKEN=ADMIN_TOKEN,
        PAYPAL_CLIENT_ID=CLIENT_ID,
        PAYPAL_CLIENT_SECRET=CLIENT_SECRET,
    )
    for rendered in (repr(s), str(s), s.model_dump_json()):
        assert ADMIN_TOKEN not in rendered
        assert CLIENT_SECRET not in rendered


@pytest.mark.parametrize(
    "bad",
    [
        {"PACT_CURRENCY": "usd"},
        {"PAYPAL_BASE_URL": "https://api-m.paypal.com"},
        {"PACT_PAYMENT_PROVIDER": "paypal", "PAYPAL_CLIENT_ID": ""},
        {"PACT_GRANT_TTL_SECONDS": "not-an-int"},
    ],
)
def test_secrets_not_in_validation_errors(monkeypatch, bad):
    env = {"PACT_ADMIN_TOKEN": ADMIN_TOKEN, "PAYPAL_CLIENT_SECRET": CLIENT_SECRET, **bad}
    with pytest.raises(ValidationError) as exc:
        load(monkeypatch, **env)
    text = error_text(exc)
    assert ADMIN_TOKEN not in text
    assert CLIENT_SECRET not in text


def test_credentials_embedded_in_base_url_not_echoed(monkeypatch):
    with pytest.raises(ValidationError) as exc:
        load(monkeypatch, PAYPAL_BASE_URL="https://user:hunter2-secret@api-m.paypal.com")
    assert "hunter2-secret" not in error_text(exc)
