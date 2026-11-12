"""PACT REST API (#12). A thin HTTP layer over PactCore and Merchant Lab — no business rules here.

Auth:
  * Agents:    ``Authorization: Bearer <agent api key>``  (routes under /v1)
  * Dashboard: ``Authorization: Bearer <PACT_ADMIN_TOKEN>`` (routes under /admin) — the only credential
    that can approve, deny, revoke for others, reconcile or change policy. Agent keys get 403 there.
  * Merchant Lab (/merchant) and the browser redirects (/payments, /sim) are unauthenticated: they are a
    sandbox commerce simulator and provider callbacks that can only act on already-authorized payments.
    ``GET /payments/cancel`` never ends a payment. ``POST /payments/abandon`` (the buyer's "Cancel this
    payment" button) is unauthenticated too, which is acceptable because it only acts on a payment
    identified by its provider order id, runs the provider-checked path (a payment the provider already
    captured is recorded as executed, not cancelled) and can only release a reservation, never move money.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

import merchant_lab.models  # noqa: F401  (register Merchant Lab tables)
from adapters.simulation.adapter import SimulationAdapter
from merchant_lab.service import MerchantLab, MerchantLabOrderSource
from pact_core.config import Settings, get_settings
from pact_core.core import PactCore
from pact_core.crypto import constant_time_equal
from pact_core.db import Clock, Database, utcnow
from pact_core.errors import Forbidden, PactError
from pact_core.money import to_minor
from pact_core.ports import PaymentAdapter
from pact_core.runtime import AgentIdentity

log = logging.getLogger("pact.server")
DASHBOARD_DIST = Path(__file__).resolve().parents[1] / "dashboard" / "dist"


# ---------------------------------------------------------------------- request bodies


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExecuteBody(_Body):
    grant_token: str
    merchant_id: str
    order_id: str
    amount_minor: int
    currency: str
    idempotency_key: str | None = None


class ApproveBody(_Body):
    request_hash: str = Field(min_length=64, max_length=64)
    note: str = Field("", max_length=500)


class DenyBody(_Body):
    reason: str = Field("", max_length=500)


class PolicyBody(_Body):
    rules: dict[str, Any]
    expected_version: int


class Item(_Body):
    product_id: str
    quantity: int = 1


class OrderBody(_Body):
    items: list[Item] = Field(min_length=1, max_length=50)
    buyer_ref: str = Field("", max_length=64)


class OrderItemsBody(_Body):
    items: list[Item] = Field(min_length=1, max_length=50)


class CartItemBody(_Body):
    product_id: str
    quantity: int = 1


class CheckoutBody(_Body):
    buyer_ref: str = Field("", max_length=64)


# ---------------------------------------------------------------------- app factory


def build_adapters(settings: Settings, public_url: str) -> dict[str, PaymentAdapter]:
    if settings.payment_provider == "paypal":
        from adapters.paypal.adapter import PayPalAdapter

        return {"paypal": PayPalAdapter(settings.paypal_client_id or "", settings.paypal_client_secret,
                                        base_url=settings.paypal_base_url,
                                        timeout=settings.paypal_timeout_seconds,
                                        landing_page=settings.paypal_landing_page)}
    return {"simulation": SimulationAdapter(public_url)}


def create_app(settings: Settings | None = None, *, adapters: dict[str, PaymentAdapter] | None = None,
               clock: Clock = utcnow, start_sweeper: bool = True, db: Database | None = None) -> FastAPI:
    settings = settings or get_settings()
    db = db or Database(settings.database_url)
    db.create_all()
    adapters = adapters if adapters is not None else build_adapters(settings, settings.public_url)
    provider = settings.payment_provider if settings.payment_provider in adapters else next(iter(adapters))
    lab = MerchantLab(db)
    core = PactCore(db, MerchantLabOrderSource(lab), adapters, provider=provider, currency=settings.currency,
                    public_url=settings.public_url, grant_ttl_seconds=settings.grant_ttl_seconds,
                    approval_ttl_seconds=settings.approval_ttl_seconds,
                    checkout_ttl_seconds=settings.checkout_ttl_seconds, clock=clock)
    admin_token = settings.admin_token.get_secret_value() if settings.admin_token else None
    sim = adapters.get("simulation")

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = None
        if start_sweeper and settings.expiry_sweep_seconds > 0:
            async def sweep():
                while True:
                    await asyncio.sleep(settings.expiry_sweep_seconds)
                    try:
                        expired = await run_in_threadpool(core.expire_stale)
                        if expired:
                            log.info("expired %d authorization(s)", len(expired))
                    except Exception:  # keep sweeping; failures are logged, never fatal
                        log.exception("expiry sweep failed")
            task = asyncio.create_task(sweep())
        yield
        if task:
            task.cancel()

    app = FastAPI(title="PACT — Policy and Authorization Control Tower", version="0.1.0", lifespan=lifespan,
                  description="Financial governance runtime for AI agents. Sandbox reference implementation.")
    app.state.core, app.state.lab, app.state.settings = core, lab, settings

    # ------------------------------------------------------------------ errors
    @app.exception_handler(PactError)
    async def pact_error(_req: Request, exc: PactError):
        return JSONResponse(status_code=exc.status_code,
                            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}})

    @app.exception_handler(RequestValidationError)
    async def validation_error(_req: Request, exc: RequestValidationError):
        errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg")} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"error": {"code": "INVALID_REQUEST",
                                                                "message": "Request failed validation",
                                                                "details": {"errors": errors}}})

    # ------------------------------------------------------------------ auth dependencies
    def _bearer(authorization: str | None) -> str:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise Forbidden("AUTH_REQUIRED", "Bearer token required", status_code=401)
        return authorization[7:].strip()

    def agent_auth(authorization: str | None = Header(None)) -> AgentIdentity:
        return core.authenticate_agent(_bearer(authorization))

    def admin_auth(authorization: str | None = Header(None)) -> str:
        token = _bearer(authorization)
        if not admin_token:
            raise Forbidden("ADMIN_DISABLED", "PACT_ADMIN_TOKEN is not configured", status_code=503)
        if not constant_time_equal(token, admin_token):
            raise Forbidden("ADMIN_AUTH_INVALID", "Admin token required for this operation")
        return "dashboard"

    # ------------------------------------------------------------------ health
    @app.get("/health", tags=["meta"])
    def health():
        return {"status": "ok", "provider": core.ctx.provider, "currency": core.ctx.currency}

    # ------------------------------------------------------------------ agent API
    @app.get("/v1/me", tags=["agent"])
    def me(agent: AgentIdentity = Depends(agent_auth)):
        return {"agent_id": agent.id, "user_id": agent.user_id, "name": agent.name}

    @app.post("/v1/authorizations", tags=["agent"], status_code=201)
    def request_authorization(body: dict, agent: AgentIdentity = Depends(agent_auth)):
        return core.request_authorization(agent, body)

    @app.post("/v1/authorizations/evaluate", tags=["agent"])
    def evaluate(body: dict, agent: AgentIdentity = Depends(agent_auth)):
        return core.evaluate_policy(agent, body)

    @app.get("/v1/authorizations/{request_id}", tags=["agent"])
    def status(request_id: str, agent: AgentIdentity = Depends(agent_auth)):
        return core.get_status(agent, request_id)

    @app.post("/v1/authorizations/{request_id}/execute", tags=["agent"])
    def execute(request_id: str, body: ExecuteBody, agent: AgentIdentity = Depends(agent_auth)):
        return core.execute(agent, {"request_id": request_id, **body.model_dump()})

    @app.post("/v1/authorizations/{request_id}/revoke", tags=["agent"])
    def revoke(request_id: str, agent: AgentIdentity = Depends(agent_auth)):
        return core.revoke(agent, request_id)

    @app.get("/v1/audit", tags=["agent"])
    def agent_audit(request_id: str | None = None, limit: int = Query(100, ge=1, le=500),
                    agent: AgentIdentity = Depends(agent_auth)):
        return {"events": core.agent_audit(agent, request_id, limit)}

    @app.get("/v1/budget", tags=["agent"])
    def agent_budget(agent: AgentIdentity = Depends(agent_auth)):
        return core.budget_status(agent)

    # ------------------------------------------------------------------ Merchant Lab
    @app.get("/merchant/merchants", tags=["merchant"])
    def merchants():
        return {"merchants": lab.list_merchants()}

    @app.get("/merchant/products", tags=["merchant"])
    def products(q: str | None = None, category: str | None = None, merchant_id: str | None = None,
                 max_price: str | None = Query(None, description="Decimal, e.g. 100.00"),
                 sort: str = "price", in_stock_only: bool = False):
        max_minor = None
        if max_price is not None:
            try:
                max_minor = to_minor(max_price)
            except ValueError:
                raise PactError("INVALID_PRICE", "max_price must be a decimal amount like 100.00") from None
        return {"products": lab.search_products(q, category=category, max_price_minor=max_minor,
                                                merchant_id=merchant_id, sort=sort, in_stock_only=in_stock_only)}

    @app.get("/merchant/products/{product_id}", tags=["merchant"])
    def product(product_id: str):
        return lab.get_product(product_id)

    @app.post("/merchant/orders", tags=["merchant"], status_code=201)
    def create_order(body: OrderBody):
        return lab.create_order([i.model_dump() for i in body.items], buyer_ref=body.buyer_ref)

    @app.get("/merchant/orders/{order_id}", tags=["merchant"])
    def get_order(order_id: str):
        return lab.get_order(order_id)

    @app.patch("/merchant/orders/{order_id}", tags=["merchant"])
    def update_order(order_id: str, body: OrderItemsBody):
        return lab.update_order_items(order_id, [i.model_dump() for i in body.items])

    @app.get("/merchant/transactions", tags=["merchant"])
    def transactions(merchant_id: str | None = None, buyer_ref: str | None = None,
                     payment_status: str | None = None, limit: int = Query(50, ge=1, le=500)):
        return {"orders": lab.list_transactions(merchant_id=merchant_id, buyer_ref=buyer_ref,
                                                payment_status=payment_status, limit=limit)}

    @app.get("/merchant/carts/{cart_id}", tags=["merchant"])
    def cart(cart_id: str):
        return lab.cart_view(cart_id)

    @app.post("/merchant/carts/{cart_id}/items", tags=["merchant"])
    def cart_add(cart_id: str, body: CartItemBody):
        return lab.cart_add(cart_id, body.product_id, body.quantity)

    @app.delete("/merchant/carts/{cart_id}/items/{product_id}", tags=["merchant"])
    def cart_remove(cart_id: str, product_id: str):
        return lab.cart_remove(cart_id, product_id)

    @app.post("/merchant/carts/{cart_id}/checkout", tags=["merchant"], status_code=201)
    def cart_checkout(cart_id: str, body: CheckoutBody | None = None):
        return lab.order_from_cart(cart_id, buyer_ref=(body.buyer_ref if body else "") or cart_id)

    # ------------------------------------------------------------------ admin API (dashboard)
    @app.get("/admin/overview", tags=["admin"])
    def overview(_admin: str = Depends(admin_auth)):
        return core.overview()

    @app.get("/admin/authorizations", tags=["admin"])
    def admin_list(state: str | None = None, limit: int = Query(100, ge=1, le=500),
                   _admin: str = Depends(admin_auth)):
        return {"authorizations": core.list_requests(state, limit)}

    @app.get("/admin/authorizations/{request_id}", tags=["admin"])
    def admin_detail(request_id: str, _admin: str = Depends(admin_auth)):
        return core.request_detail(request_id)

    @app.post("/admin/authorizations/{request_id}/approve", tags=["admin"])
    def approve(request_id: str, body: ApproveBody, admin: str = Depends(admin_auth)):
        return core.approve(admin, request_id, body.request_hash, body.note)

    @app.post("/admin/authorizations/{request_id}/deny", tags=["admin"])
    def deny(request_id: str, body: DenyBody, admin: str = Depends(admin_auth)):
        return core.deny(admin, request_id, body.reason)

    @app.post("/admin/authorizations/{request_id}/revoke", tags=["admin"])
    def admin_revoke(request_id: str, admin: str = Depends(admin_auth)):
        return core.admin_revoke(admin, request_id)

    @app.post("/admin/authorizations/{request_id}/cancel", tags=["admin"])
    def cancel_payment(request_id: str, admin: str = Depends(admin_auth)):
        return core.cancel_payment(admin, request_id)

    @app.post("/admin/authorizations/{request_id}/reconcile", tags=["admin"])
    def reconcile(request_id: str, _admin: str = Depends(admin_auth)):
        return core.reconcile(request_id)

    @app.get("/admin/policies", tags=["admin"])
    def policies(_admin: str = Depends(admin_auth)):
        return {"policies": core.list_policies()}

    @app.put("/admin/policies/{policy_id}", tags=["admin"])
    def update_policy(policy_id: str, body: PolicyBody, admin: str = Depends(admin_auth)):
        return core.update_policy(admin, policy_id, body.rules, body.expected_version)

    @app.get("/admin/agents", tags=["admin"])
    def agents(_admin: str = Depends(admin_auth)):
        return {"agents": core.list_agents()}

    @app.get("/admin/budgets", tags=["admin"])
    def budgets(_admin: str = Depends(admin_auth)):
        return core.budget_status()

    @app.get("/admin/audit", tags=["admin"])
    def audit(request_id: str | None = None, event_type: str | None = None,
              limit: int = Query(200, ge=1, le=500), _admin: str = Depends(admin_auth)):
        return {"events": core.audit_events(request_id=request_id, event_type=event_type, limit=limit)}

    @app.get("/admin/audit/verify", tags=["admin"])
    def audit_verify(_admin: str = Depends(admin_auth)):
        return core.verify_audit()

    # ------------------------------------------------------------------ provider redirects (browser)
    @app.get("/payments/return", tags=["payments"], response_class=HTMLResponse)
    def payment_return(token: str = Query(..., max_length=64)):
        try:
            view = core.handle_provider_return(token)
        except PactError as exc:
            return _page("Payment not found", f"<p>{html.escape(exc.message)}</p>", status=404)
        return _result_page(view)

    @app.get("/payments/cancel", tags=["payments"], response_class=HTMLResponse)
    def payment_cancel(token: str = Query(..., max_length=64)):
        try:
            view = core.handle_provider_cancel(token)  # non-terminal: a GET never ends a payment
        except PactError as exc:
            return _page("Payment not found", f"<p>{html.escape(exc.message)}</p>", status=404)
        return _result_page(view)

    @app.post("/payments/abandon", tags=["payments"], response_class=HTMLResponse)
    async def payment_abandon(request: Request):
        """The buyer's explicit "Cancel this payment". Form field ``token`` = provider order id."""
        token = await _form_token(request)
        try:
            view = await run_in_threadpool(core.buyer_abandon, token)
        except PactError as exc:
            title = "Payment not found" if exc.status_code == 404 else "Payment could not be cancelled"
            return _page(title, f"<p>{html.escape(exc.message)}</p>", status=exc.status_code)
        return _result_page(view)

    # ------------------------------------------------------------------ simulated checkout (simulation only)
    if isinstance(sim, SimulationAdapter):
        @app.get("/sim/checkout/{order_id}", tags=["simulation"], response_class=HTMLResponse)
        def sim_checkout(order_id: str):
            o = sim.lookup(order_id)
            if o is None:
                return _page("Unknown checkout", "<p>No such simulated order.</p>", status=404)
            oid = html.escape(o.id)
            body = (f"<p class='warn'>SIMULATED CHECKOUT — this is not PayPal and no money moves.</p>"
                    f"<p>Order <code>{oid}</code>: <strong>{o.amount_minor / 100:.2f} {html.escape(o.currency)}</strong>"
                    f" — status {html.escape(o.status)}</p>"
                    f"<form method='post' action='/sim/checkout/{oid}/approve'><button>Approve payment</button></form>"
                    f"<form method='post' action='/sim/checkout/{oid}/cancel'><button class='alt'>Cancel</button></form>")
            return _page("Simulated checkout", body)

        @app.post("/sim/checkout/{order_id}/approve", tags=["simulation"])
        def sim_approve(order_id: str):
            o = sim.lookup(order_id)
            if o is None:
                return _page("Unknown checkout", "<p>No such simulated order.</p>", status=404)
            try:
                sim.buyer_approve(order_id)
            except Exception as exc:  # e.g. already completed
                return _page("Cannot approve", f"<p>{html.escape(str(exc))}</p>", status=409)
            return RedirectResponse(f"{o.return_url}?token={order_id}", status_code=303)

        @app.post("/sim/checkout/{order_id}/cancel", tags=["simulation"])
        def sim_cancel(order_id: str):
            o = sim.lookup(order_id)
            if o is None:
                return _page("Unknown checkout", "<p>No such simulated order.</p>", status=404)
            # Like PayPal's "cancel and return" link: the buyer just leaves. The order is not voided, so
            # "Try again" on the cancel page still leads to a payable checkout.
            return RedirectResponse(f"{o.cancel_url}?token={order_id}", status_code=303)

    # ------------------------------------------------------------------ dashboard (built SPA) or index
    if (DASHBOARD_DIST / "index.html").exists():
        app.mount("/", StaticFiles(directory=DASHBOARD_DIST, html=True), name="dashboard")
    else:
        @app.get("/", include_in_schema=False)
        def index():
            return {"service": "PACT", "docs": "/docs", "health": "/health"}

    return app


# ---------------------------------------------------------------------- HTML helpers

_STYLE = """body{font-family:system-ui,sans-serif;max-width:560px;margin:48px auto;padding:0 16px;color:#1b1f24}
h1{font-size:1.4rem}.warn{background:#fff4ce;border:1px solid #e6c200;padding:8px 12px;border-radius:6px}
.ok{color:#0a7a32}.bad{color:#b42318}code{background:#f3f4f6;padding:1px 4px;border-radius:4px}
button{font-size:1rem;padding:10px 18px;margin:8px 8px 0 0;border-radius:6px;border:0;background:#1f6feb;color:#fff;cursor:pointer}
button.alt{background:#6b7280}a.btn{display:inline-block;padding:10px 18px;margin:8px 8px 0 0;border-radius:6px;background:#1f6feb;color:#fff;text-decoration:none}dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px}dt{color:#6b7280}"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' "
                        f"content='width=device-width,initial-scale=1'><title>{html.escape(title)} · PACT</title>"
                        f"<style>{_STYLE}</style></head><body><h1>{html.escape(title)}</h1>{body}</body></html>",
                        status_code=status)


TOKEN_MAX_LENGTH = 64  # same limit as the ?token= query parameter on the provider redirects
_FORM_MAX_BYTES = 1024


async def _form_token(request: Request) -> str:
    """Read the ``token`` field of a small urlencoded form body (no multipart dependency needed)."""
    def invalid(msg: str) -> RequestValidationError:
        return RequestValidationError([{"loc": ("body", "token"), "msg": msg, "type": "value_error"}])

    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > _FORM_MAX_BYTES:
        raise invalid("Form body too large")
    raw = await request.body()
    if len(raw) > _FORM_MAX_BYTES:
        raise invalid("Form body too large")
    try:
        values = parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=8).get("token", [])
    except ValueError:  # not UTF-8, or too many fields
        raise invalid("Malformed form body") from None
    if len(values) != 1 or not values[0]:
        raise invalid("Field required")
    if len(values[0]) > TOKEN_MAX_LENGTH:
        raise invalid(f"String should have at most {TOKEN_MAX_LENGTH} characters")
    return values[0]


def _checkout_link(ex: dict, label: str) -> str:
    """A link back to the provider checkout. The URL only ever comes from the stored view and must be an
    absolute http(s) URL; anything else (missing, ``javascript:``, …) renders no link at all."""
    url = ex.get("approval_url")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        return ""
    return f"<p><a class='btn' href=\"{html.escape(url, quote=True)}\">{html.escape(label)}</a></p>"


def _sandbox_hint(*, payment_open: bool) -> str:
    retry = "" if payment_open else " Ask your agent to start a new purchase to try again."
    return ("<p class='warn'>Checkout was cancelled at PayPal. In PayPal <strong>Sandbox</strong>, either log in "
            "with the exact <strong>personal</strong> sandbox account email and password (developer.paypal.com → "
            "Testing Tools → Sandbox Accounts → <code>sb-…@personal.example.com</code> → View/edit account) — if "
            "PayPal shows a debit/credit card form after you enter an email, it did not recognise that email — "
            "or pay on that card form with a test card from Testing Tools → Credit Card Generator (any future "
            f"expiry, any CVV). Never use the business account that receives the payment.{retry} "
            "See <code>docs/paypal-sandbox-setup.md</code>.</p>")


def _result_page(view: dict) -> HTMLResponse:
    state = view["state"]
    ex = view["execution"]
    reason = ex.get("failure_reason") or ""
    is_paypal = ex.get("provider") == "paypal"
    open_status = ex.get("provider_status") if state == "EXECUTING" else None
    headline = {"EXECUTED": "Payment complete", "FAILED": "Payment not completed",
                "UNKNOWN": "Payment being verified"}.get(state, "Payment in progress")
    cls = "ok" if state == "EXECUTED" else ("bad" if state == "FAILED" else "")
    rows = {"Status": state, "Provider": ex.get("provider_label"), "Amount": f"{view['amount']} {view['currency']}",
            "Order": view["order_id"], "Authorization": view["request_id"],
            "Capture id": ex.get("capture_id"), "Reason": reason}
    dl = "".join(f"<dt>{html.escape(k)}</dt><dd>{html.escape(str(v))}</dd>" for k, v in rows.items() if v)
    note, actions, hint = "", "", ""
    closing = "<p>You can close this tab — your agent picks up the result automatically.</p>"

    if open_status == "BUYER_CANCELLED":  # buyer left the checkout; the payment is still open and payable
        headline = "Checkout cancelled"
        note = "<p>Nothing was charged. The payment is still open — you can try again or cancel it.</p>"
        token = html.escape(str(ex.get("provider_order_id") or ""), quote=True)
        actions = (_checkout_link(ex, "Try again")
                   + "<form method='post' action='/payments/abandon'>"
                     f"<input type='hidden' name='token' value=\"{token}\">"
                     "<button class='alt'>Cancel this payment</button></form>")
        hint = _sandbox_hint(payment_open=True) if is_paypal else ""
        closing = "<p>If you do nothing, the payment stays open until it is completed, cancelled or times out.</p>"
    elif open_status == "INSTRUMENT_DECLINED":
        headline = "Payment method declined"
        note = ("<p>PayPal declined the payment method. Nothing was charged. The payment is still open — "
                "go back to the checkout and choose another payment method.</p>")
        actions = _checkout_link(ex, "Choose another payment method")
        closing = "<p>If you do nothing, the payment stays open until it is completed, cancelled or times out.</p>"
    elif reason.startswith("CAPTURE_REJECTED:"):
        code = html.escape(reason.split(":", 1)[1])
        note = (f"<p>PayPal refused the payment and no money moved. Provider code: <code>{code}</code>.</p>")
    elif reason == "CHECKOUT_TIMEOUT":
        note = "<p>The checkout was not completed in time. Nothing was charged.</p>"
    elif reason == "PROVIDER_ORDER_NOT_FOUND":
        note = "<p>The PayPal order expired before it was paid. Nothing was charged.</p>"
    elif reason == "BUYER_CANCELLED":
        note = "<p>The payment was cancelled. Nothing was charged.</p>"
        hint = _sandbox_hint(payment_open=False) if is_paypal else ""

    return _page(headline, f"<p class='{cls}'><strong>{html.escape(state)}</strong></p>{note}<dl>{dl}</dl>"
                           f"{actions}{hint}{closing}")
