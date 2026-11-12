"""Operator conveniences behind the ``pact`` CLI: server lock, doctor checks, terminal approvals, demo setup.

The approval client uses the admin token from the operator's environment (.env). It is the human side of
PACT; agents are never given that token, and the MCP server instructs them never to run these commands.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from pact_core.config import Settings

PACT_DIR = Path(".pact")
LOCK_FILE = PACT_DIR / "server.lock"
KEYS_FILE = PACT_DIR / "agent_keys.json"
SHOPPING_AGENT = "ag_shopping"


# ---------------------------------------------------------------------- server lock


class ServerAlreadyRunning(RuntimeError):
    pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_lock(lock_file: Path = LOCK_FILE) -> dict | None:
    try:
        data = json.loads(lock_file.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and _pid_alive(int(data.get("pid", -1))) else None


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host if host not in ("0.0.0.0", "") else "127.0.0.1", port)) == 0


def acquire_lock(host: str, port: int, provider: str, lock_file: Path = LOCK_FILE) -> None:
    """Refuse to start a second PACT server (two servers on one DB/port caused provider flip-flopping)."""
    running = read_lock(lock_file)
    if running:
        raise ServerAlreadyRunning(
            f"PACT is already running (pid {running['pid']}, port {running['port']}, provider {running['provider']}). "
            "Use it, or run `pact stop` first.")
    if port_in_use(host, port):
        raise ServerAlreadyRunning(f"Port {port} is already in use by another process. Run `pact doctor` to see what it is.")
    lock_file.parent.mkdir(exist_ok=True)
    lock_file.write_text(json.dumps({"pid": os.getpid(), "port": port, "host": host, "provider": provider,
                                     "started_at": datetime.now(UTC).isoformat()}))


def release_lock(lock_file: Path = LOCK_FILE) -> None:
    try:
        data = json.loads(lock_file.read_text())
        if int(data.get("pid", -1)) == os.getpid():
            lock_file.unlink()
    except (OSError, ValueError):
        pass


def stop_server(lock_file: Path = LOCK_FILE, timeout: float = 10.0) -> str:
    running = read_lock(lock_file)
    if not running:
        if lock_file.exists():
            lock_file.unlink()
        return "No PACT server is running."
    pid = int(running["pid"])
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.2)
    if lock_file.exists() and not _pid_alive(pid):
        lock_file.unlink()
    return f"Stopped PACT server (pid {pid})." if not _pid_alive(pid) else f"Sent SIGTERM to pid {pid}; still running."


# ---------------------------------------------------------------------- doctor


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""
    warn_only: bool = False


def mcp_registered(cwd: Path | None = None) -> bool | None:
    """True/False if the `claude` CLI is available, None if it isn't installed."""
    if shutil.which("claude") is None:
        return None
    try:
        r = subprocess.run(["claude", "mcp", "get", "pact"], cwd=cwd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.returncode == 0


def run_doctor(settings: Settings, *, env_file: Path = Path(".env"), check_mcp: bool = True,
               http: httpx.Client | None = None, shopper_dir: Path | None = None) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check(".env file", env_file.exists(), str(env_file) if env_file.exists() else "missing",
                        "cp .env.example .env"))
    checks.append(Check("Admin token", bool(settings.admin_token), "set" if settings.admin_token else "missing",
                        "set PACT_ADMIN_TOKEN in .env (a long random string)"))

    # Payment provider
    if settings.payment_provider == "paypal":
        from adapters.paypal.adapter import PayPalAdapter
        from pact_core.ports import ProviderError, ProviderTimeout

        try:
            PayPalAdapter(settings.paypal_client_id or "", settings.paypal_client_secret,
                          base_url=settings.paypal_base_url, timeout=15).verify_credentials()
            checks.append(Check("PayPal Sandbox credentials", True, "OAuth OK"))
        except (ProviderError, ProviderTimeout, ValueError) as exc:
            checks.append(Check("PayPal Sandbox credentials", False, getattr(exc, "code", type(exc).__name__),
                                "check PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET (Sandbox app) in .env"))
    else:
        checks.append(Check("Payment provider", True, "simulation (payments are NOT PayPal)",
                            "set PACT_PAYMENT_PROVIDER=paypal for the real PayPal Sandbox demo", warn_only=True))

    # Database
    try:
        import merchant_lab.models  # noqa: F401
        from sqlalchemy import func, select

        from pact_core.db import Database
        from pact_core.models import Agent, Policy

        db = Database(settings.database_url)
        db.create_all()
        with db.transaction() as s:
            n_pol = s.execute(select(func.count()).select_from(Policy)).scalar_one()
            n_ag = s.execute(select(func.count()).select_from(Agent)).scalar_one()
        checks.append(Check("Database seeded", n_pol > 0 and n_ag > 0, f"{n_pol} policies, {n_ag} agents",
                            "run `pact demo` (or `pact init`)"))
    except Exception as exc:  # report, don't crash the doctor
        checks.append(Check("Database seeded", False, type(exc).__name__, f"check PACT_DATABASE_URL ({settings.database_url})"))

    keys = load_keys()
    checks.append(Check("Agent keys file", SHOPPING_AGENT in keys,
                        str(KEYS_FILE) if keys else "missing",
                        "run `pact demo` (it re-issues the shopping agent key if the file is lost)"))

    # Running server
    lock = read_lock()
    client = http or httpx.Client(base_url=settings.public_url, timeout=3)
    try:
        health = client.get("/health").json()
        match = health.get("provider") == settings.payment_provider
        checks.append(Check("PACT server", match,
                            f"running at {settings.public_url}, provider {health.get('provider')}",
                            f".env says provider={settings.payment_provider} but the running server uses "
                            f"{health.get('provider')}: run `pact stop` then `pact serve`"))
    except (httpx.HTTPError, ValueError):
        checks.append(Check("PACT server", False, f"not reachable at {settings.public_url}", "run `pact serve` (or `pact demo`)"))
    if lock:
        checks.append(Check("Server lock", True, f"pid {lock['pid']} on port {lock['port']} ({lock['provider']})"))

    if check_mcp:
        reg = mcp_registered(shopper_dir)
        where = f" for {shopper_dir}" if shopper_dir else ""
        if reg is None:
            checks.append(Check("Claude Code MCP", False, "`claude` CLI not found", "install Claude Code", warn_only=True))
        else:
            checks.append(Check("Claude Code MCP", reg, f"'pact' registered{where}" if reg else f"'pact' not registered{where}",
                                "run `pact demo` to register it", warn_only=not reg and shopper_dir is None))
    return checks


def format_checks(checks: list[Check]) -> tuple[str, bool]:
    lines, healthy = [], True
    for c in checks:
        mark = "✓" if c.ok else ("!" if c.warn_only else "✗")
        healthy &= c.ok or c.warn_only
        lines.append(f" {mark} {c.name:28s} {c.detail}")
        if not c.ok and c.fix:
            lines.append(f"   → {c.fix}")
        elif c.ok and c.warn_only and c.fix:
            lines.append(f"   → {c.fix}")
    return "\n".join(lines), healthy


# ---------------------------------------------------------------------- keys


def load_keys() -> dict[str, str]:
    try:
        return json.loads(KEYS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_keys(keys: dict[str, str]) -> None:
    PACT_DIR.mkdir(exist_ok=True)
    merged = {**load_keys(), **keys}
    fd = os.open(KEYS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(merged, f, indent=2)


# ---------------------------------------------------------------------- approvals (human side)


class ApprovalClient:
    def __init__(self, http: httpx.Client):
        self.http = http

    @classmethod
    def from_settings(cls, settings: Settings) -> "ApprovalClient":
        if not settings.admin_token:
            raise RuntimeError("PACT_ADMIN_TOKEN is not set in .env")
        return cls(httpx.Client(base_url=settings.public_url, timeout=30,
                                headers={"Authorization": f"Bearer {settings.admin_token.get_secret_value()}"}))

    def _json(self, r: httpx.Response) -> dict:
        body = r.json()
        if r.status_code >= 400:
            err = body.get("error", {})
            raise RuntimeError(f"{err.get('code', r.status_code)}: {err.get('message', '')}")
        return body

    def pending(self) -> list[dict]:
        return self._json(self.http.get("/admin/authorizations", params={"state": "PENDING_APPROVAL"}))["authorizations"]

    def detail(self, request_id: str) -> dict:
        return self._json(self.http.get(f"/admin/authorizations/{request_id}"))

    def approve(self, request_id: str, request_hash: str, note: str = "approved from terminal") -> dict:
        return self._json(self.http.post(f"/admin/authorizations/{request_id}/approve",
                                         json={"request_hash": request_hash, "note": note}))

    def cancel(self, request_id: str) -> dict:
        return self._json(self.http.post(f"/admin/authorizations/{request_id}/cancel"))

    def stuck(self) -> list[dict]:
        out = []
        for state in ("EXECUTING", "UNKNOWN"):
            out += self._json(self.http.get("/admin/authorizations", params={"state": state}))["authorizations"]
        return out

    def deny(self, request_id: str, reason: str) -> dict:
        return self._json(self.http.post(f"/admin/authorizations/{request_id}/deny", json={"reason": reason}))


def describe(detail: dict) -> str:
    items = ", ".join(f"{i['quantity']}× {i['name']}" for i in (detail.get("order_snapshot") or {}).get("items", []))
    lines = [f"{detail['request_id']}   {detail['amount']} {detail['currency']}",
             f"  agent     {detail['agent_id']}",
             f"  merchant  {detail.get('merchant_name') or ''} ({detail['merchant_id']})",
             f"  order     {detail['order_id']}: {items}",
             f"  expires   {detail.get('approval_expires_at')}"]
    for f in (detail.get("decision") or {}).get("findings", []):
        lines.append(f"  [{f['effect']}] {f['code']}: {f['message']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------- demo shopper workspace

SHOPPER_CLAUDE_MD = """# Shopping agent workspace (PACT demo)

You are a shopping agent. You buy things for the user only through the `pact` MCP tools.

- Every payment is governed by PACT. Report its decisions and reason codes exactly as returned.
- You cannot approve payments. When a request is PENDING_APPROVAL, tell the user it is waiting for their
  approval and call `pact_wait_for_status`; continue automatically when it returns.
- After `pact_execute_action`, give the user the checkout link, then call `pact_wait_for_status` until the
  payment is EXECUTED (or report the exact failure).
- Never edit PACT's configuration, start or stop its server, or run its admin commands. If PACT is
  unreachable, ask the user to run `pact doctor`.
"""


def prepare_shopper_dir(path: Path) -> None:
    """A separate workspace for the shopping agent, outside the PACT repo (and away from PACT's .env)."""
    path.mkdir(parents=True, exist_ok=True)
    md = path / "CLAUDE.md"
    if not md.exists():
        md.write_text(SHOPPER_CLAUDE_MD)


def register_mcp(shopper_dir: Path, project_dir: Path, api_url: str, agent_key: str, *, replace: bool) -> str:
    if shutil.which("claude") is None:
        return "Claude Code CLI not found; register manually (see examples/claude_code/README.md)."
    if replace:
        subprocess.run(["claude", "mcp", "remove", "pact", "-s", "local"], cwd=shopper_dir, capture_output=True,
                       text=True, timeout=60)
    elif mcp_registered(shopper_dir):
        return f"MCP server 'pact' already registered for {shopper_dir}."
    r = subprocess.run(["claude", "mcp", "add", "pact", "-s", "local", "-e", f"PACT_API_URL={api_url}",
                        "-e", f"PACT_AGENT_KEY={agent_key}", "--", "uv", "run", "--directory", str(project_dir),
                        "pact-mcp"], cwd=shopper_dir, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return "Could not register the MCP server automatically; see examples/claude_code/README.md."
    return f"Registered MCP server 'pact' for {shopper_dir} (Claude Code local scope)."
