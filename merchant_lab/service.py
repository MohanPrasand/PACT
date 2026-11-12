"""Merchant Lab: product catalogue, cart and orders. It creates commercial orders; it never authorises
or executes payments — that is PACT's job.

Rules:

* Prices come only from the catalogue. Order items carry ``product_id`` and ``quantity``; any other
  field (``unit_price_minor``, ``price``, ...) is rejected with ``INVALID_ITEM``.
* An order holds items from exactly one merchant, in one currency.
* Payment status changes only through :meth:`MerchantLabOrderSource.set_payment_status`, which PACT
  calls. ``CAPTURED`` is terminal and requires a provider reference.
* Every public method opens its own transaction. Never call Merchant Lab while a PACT transaction is
  open: on SQLite both would compete for the single writer lock (see ``pact_core.db``).
"""

from __future__ import annotations

from sqlalchemy import or_, select

from merchant_lab.models import CartItem, Merchant, Order, Product
from pact_core.crypto import new_id
from pact_core.db import Database, utcnow
from pact_core.errors import Conflict, InvalidTransition, NotFound, PactError
from pact_core.policy.engine import OrderFacts

PAYMENT_STATUSES = frozenset({"CREATED", "PENDING", "APPROVED", "CAPTURED", "FAILED", "CANCELLED"})
#: Statuses in which the order's items may still change (nothing is in flight or paid).
MUTABLE_STATUSES = frozenset({"CREATED", "FAILED", "CANCELLED"})
#: Allowed payment-status transitions. Setting the current status again is an idempotent no-op.
#: APPROVED (buyer approved at the provider, capture not yet done) is optional between PENDING and
#: the outcome. FAILED/CANCELLED -> PENDING is a retry with a new authorization.
PAYMENT_TRANSITIONS: dict[str, frozenset[str]] = {
    "CREATED": frozenset({"PENDING"}),
    "PENDING": frozenset({"APPROVED", "CAPTURED", "FAILED", "CANCELLED"}),
    "APPROVED": frozenset({"CAPTURED", "FAILED", "CANCELLED"}),
    "FAILED": frozenset({"PENDING"}),
    "CANCELLED": frozenset({"PENDING"}),
    "CAPTURED": frozenset(),
}
#: Statuses in which one payment attempt is in flight and bound to its authorization.
IN_FLIGHT_STATUSES = frozenset({"PENDING", "APPROVED"})

SORT_OPTIONS = ("price", "price_desc", "rating", "name")
ITEM_FIELDS = frozenset({"product_id", "quantity"})
MAX_LIST_LIMIT = 500


def product_dict(p: Product) -> dict:
    return {
        "product_id": p.id, "merchant_id": p.merchant_id, "name": p.name, "description": p.description,
        "category": p.category, "price_minor": p.price_minor, "currency": p.currency,
        "price": f"{p.price_minor / 100:.2f}", "stock": p.stock, "rating": p.rating,
    }


def order_dict(o: Order, merchant_name: str | None = None) -> dict:
    return {
        "order_id": o.id, "merchant_id": o.merchant_id, "merchant_name": merchant_name, "buyer_ref": o.buyer_ref,
        "items": o.items, "total_minor": o.total_minor, "total": f"{o.total_minor / 100:.2f}", "currency": o.currency,
        "payment_status": o.payment_status, "authorization_id": o.authorization_id, "provider_ref": o.provider_ref,
        "created_at": o.created_at.isoformat() + "Z", "updated_at": o.updated_at.isoformat() + "Z",
    }


def _check_quantity(quantity) -> int:
    # bool is an int subclass; True must not mean "1".
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise PactError("INVALID_QUANTITY", "Quantity must be a whole number of at least 1",
                        details={"quantity": repr(quantity)})
    return quantity


def _escape_like(word: str) -> str:
    return word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class MerchantLab:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------- catalogue
    def search_products(self, query: str | None = None, category: str | None = None,
                        max_price_minor: int | None = None, merchant_id: str | None = None,
                        sort: str = "price", in_stock_only: bool = False) -> list[dict]:
        """Case-insensitive search. Every word in ``query`` must appear in the name, description or
        category. ``sort``: ``price`` (cheapest first), ``price_desc``, ``rating`` (best first, then
        cheapest) or ``name``."""
        if sort not in SORT_OPTIONS:
            raise PactError("INVALID_SORT", f"sort must be one of {', '.join(SORT_OPTIONS)}")
        if max_price_minor is not None and (isinstance(max_price_minor, bool) or not isinstance(max_price_minor, int)
                                            or max_price_minor < 0):
            raise PactError("INVALID_PRICE_FILTER", "max_price_minor must be a non-negative integer")
        with self.db.transaction() as s:
            stmt = select(Product)
            for word in (query or "").lower().split():
                like = f"%{_escape_like(word)}%"
                stmt = stmt.where(or_(Product.name.ilike(like, escape="\\"),
                                      Product.description.ilike(like, escape="\\"),
                                      Product.category.ilike(like, escape="\\")))
            if category:
                stmt = stmt.where(Product.category == category.lower())
            if max_price_minor is not None:
                stmt = stmt.where(Product.price_minor <= max_price_minor)
            if merchant_id:
                stmt = stmt.where(Product.merchant_id == merchant_id)
            if in_stock_only:
                stmt = stmt.where(Product.stock > 0)
            order_by = {
                "price": (Product.price_minor.asc(), Product.id),
                "price_desc": (Product.price_minor.desc(), Product.id),
                "rating": (Product.rating.desc(), Product.price_minor.asc(), Product.id),
                "name": (Product.name.asc(), Product.id),
            }[sort]
            return [product_dict(p) for p in s.execute(stmt.order_by(*order_by)).scalars()]

    def get_product(self, product_id: str) -> dict:
        with self.db.transaction() as s:
            p = s.get(Product, product_id)
            if not p:
                raise NotFound("PRODUCT_NOT_FOUND", f"No product {product_id}")
            return product_dict(p)

    def list_merchants(self) -> list[dict]:
        with self.db.transaction() as s:
            return [{"merchant_id": m.id, "name": m.name, "description": m.description}
                    for m in s.execute(select(Merchant).order_by(Merchant.id)).scalars()]

    # ------------------------------------------------------------- cart
    def cart_add(self, cart_id: str, product_id: str, quantity: int = 1) -> dict:
        _check_quantity(quantity)
        with self.db.transaction() as s:
            p = s.get(Product, product_id)
            if not p:
                raise NotFound("PRODUCT_NOT_FOUND", f"No product {product_id}")
            item = s.execute(select(CartItem).where(CartItem.cart_id == cart_id,
                                                    CartItem.product_id == product_id)).scalar_one_or_none()
            new_qty = (item.quantity if item else 0) + quantity
            if new_qty > p.stock:
                raise Conflict("OUT_OF_STOCK", f"{p.name} has only {p.stock} in stock",
                               details={"product_id": p.id, "stock": p.stock, "requested": new_qty})
            if item:
                item.quantity = new_qty
            else:
                s.add(CartItem(cart_id=cart_id, product_id=product_id, quantity=quantity))
            s.flush()
            return self._cart_view(s, cart_id)

    def cart_remove(self, cart_id: str, product_id: str) -> dict:
        """Remove a product line from the cart (all of its quantity). Removing an absent line is a no-op."""
        with self.db.transaction() as s:
            for item in s.execute(select(CartItem).where(CartItem.cart_id == cart_id,
                                                         CartItem.product_id == product_id)).scalars():
                s.delete(item)
            s.flush()
            return self._cart_view(s, cart_id)

    def cart_view(self, cart_id: str) -> dict:
        with self.db.transaction() as s:
            return self._cart_view(s, cart_id)

    def _cart_view(self, s, cart_id: str) -> dict:
        lines = []
        stmt = select(CartItem).where(CartItem.cart_id == cart_id).order_by(CartItem.id)
        for item in s.execute(stmt).scalars():
            p = s.get(Product, item.product_id)
            lines.append({"product_id": p.id, "name": p.name, "merchant_id": p.merchant_id, "quantity": item.quantity,
                          "unit_price_minor": p.price_minor, "line_total_minor": p.price_minor * item.quantity,
                          "currency": p.currency})
        currencies = {x["currency"] for x in lines}
        return {"cart_id": cart_id, "items": lines,
                "item_count": sum(x["quantity"] for x in lines),
                "total_minor": sum(x["line_total_minor"] for x in lines),
                "currency": currencies.pop() if len(currencies) == 1 else None}

    def order_from_cart(self, cart_id: str, buyer_ref: str = "") -> dict:
        """Create an order from the cart and empty the cart, atomically. The cart is kept if the order
        cannot be created (e.g. mixed merchants or out of stock)."""
        with self.db.transaction() as s:
            items = list(s.execute(select(CartItem).where(CartItem.cart_id == cart_id).order_by(CartItem.id)).scalars())
            if not items:
                raise PactError("CART_EMPTY", "Cart is empty")
            order = self._create_order(s, [{"product_id": i.product_id, "quantity": i.quantity} for i in items],
                                       buyer_ref)
            for item in items:
                s.delete(item)
            return order

    # ------------------------------------------------------------- orders
    def _price_items(self, s, items: list[dict]) -> tuple[str, list[dict], int, str]:
        """Price items from the catalogue. Duplicate product lines are merged before the stock check."""
        if not isinstance(items, list) or not items:
            raise PactError("EMPTY_ORDER", "An order needs at least one item")
        quantities: dict[str, int] = {}
        for it in items:
            if not isinstance(it, dict):
                raise PactError("INVALID_ITEM", "Each item must be an object with product_id and quantity")
            extra = set(it) - ITEM_FIELDS
            if extra:
                raise PactError("INVALID_ITEM",
                                f"Unsupported item field(s): {', '.join(sorted(extra))}. Prices are set by the "
                                "merchant; send only product_id and quantity.", details={"fields": sorted(extra)})
            product_id = it.get("product_id")
            if not isinstance(product_id, str) or not product_id:
                raise PactError("INVALID_ITEM", "product_id is required")
            qty = _check_quantity(it.get("quantity", 1))
            quantities[product_id] = quantities.get(product_id, 0) + qty

        lines, merchant_ids, currencies = [], set(), set()
        for product_id, qty in quantities.items():
            p = s.get(Product, product_id)
            if not p:
                raise NotFound("PRODUCT_NOT_FOUND", f"No product {product_id}")
            if p.stock < qty:
                raise Conflict("OUT_OF_STOCK", f"{p.name} has only {p.stock} in stock",
                               details={"product_id": p.id, "stock": p.stock, "requested": qty})
            merchant_ids.add(p.merchant_id)
            currencies.add(p.currency)
            lines.append({"product_id": p.id, "name": p.name, "category": p.category, "quantity": qty,
                          "unit_price_minor": p.price_minor})
        if len(merchant_ids) != 1 or len(currencies) != 1:
            raise PactError("MIXED_ORDER", "All items in an order must come from one merchant in one currency",
                            details={"merchant_ids": sorted(merchant_ids), "currencies": sorted(currencies)})
        total = sum(line["unit_price_minor"] * line["quantity"] for line in lines)
        return merchant_ids.pop(), lines, total, currencies.pop()

    def _create_order(self, s, items: list[dict], buyer_ref: str) -> dict:
        merchant_id, lines, total, currency = self._price_items(s, items)
        o = Order(id=new_id("ord"), merchant_id=merchant_id, buyer_ref=buyer_ref, items=lines,
                  total_minor=total, currency=currency, payment_status="CREATED")
        s.add(o)
        s.flush()
        return order_dict(o, s.get(Merchant, merchant_id).name)

    def create_order(self, items: list[dict], buyer_ref: str = "") -> dict:
        """Create an order from ``[{"product_id", "quantity"}]``. Prices and totals come from the catalogue."""
        with self.db.transaction() as s:
            return self._create_order(s, items, buyer_ref)

    def update_order_items(self, order_id: str, items: list[dict]) -> dict:
        """Change an unpaid order. PACT detects this as transaction modification after approval."""
        with self.db.transaction() as s:
            o = s.get(Order, order_id, with_for_update=True)
            if not o:
                raise NotFound("ORDER_NOT_FOUND", f"No order {order_id}")
            if o.payment_status not in MUTABLE_STATUSES:
                raise Conflict("ORDER_LOCKED", f"Order is {o.payment_status} and can no longer be modified")
            merchant_id, lines, total, currency = self._price_items(s, items)
            if merchant_id != o.merchant_id:
                raise PactError("MIXED_ORDER", "Items must come from the order's merchant")
            o.items, o.total_minor, o.currency, o.updated_at = lines, total, currency, utcnow()
            return order_dict(o, s.get(Merchant, o.merchant_id).name)

    def get_order(self, order_id: str) -> dict:
        with self.db.transaction() as s:
            o = s.get(Order, order_id)
            if not o:
                raise NotFound("ORDER_NOT_FOUND", f"No order {order_id}")
            return order_dict(o, s.get(Merchant, o.merchant_id).name)

    def list_transactions(self, merchant_id: str | None = None, buyer_ref: str | None = None,
                          limit: int = 50, payment_status: str | None = None) -> list[dict]:
        """Orders, newest first, optionally filtered by merchant, buyer and payment status."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIST_LIMIT:
            raise PactError("INVALID_LIMIT", f"limit must be between 1 and {MAX_LIST_LIMIT}")
        if payment_status is not None and payment_status not in PAYMENT_STATUSES:
            raise PactError("INVALID_PAYMENT_STATUS", f"Unknown payment status {payment_status}")
        with self.db.transaction() as s:
            stmt = select(Order).order_by(Order.created_at.desc(), Order.id).limit(limit)
            if merchant_id:
                stmt = stmt.where(Order.merchant_id == merchant_id)
            if buyer_ref:
                stmt = stmt.where(Order.buyer_ref == buyer_ref)
            if payment_status:
                stmt = stmt.where(Order.payment_status == payment_status)
            names = {m.id: m.name for m in s.execute(select(Merchant)).scalars()}
            return [order_dict(o, names.get(o.merchant_id)) for o in s.execute(stmt).scalars()]


class MerchantLabOrderSource:
    """Adapter exposing Merchant Lab orders to PACT Core through the OrderSource port."""

    def __init__(self, lab: MerchantLab):
        self.lab = lab

    def get_order(self, order_id: str) -> OrderFacts | None:
        with self.lab.db.transaction() as s:
            o = s.get(Order, order_id)
            if not o:
                return None
            return OrderFacts(order_id=o.id, merchant_id=o.merchant_id, amount_minor=o.total_minor,
                              currency=o.currency, status=o.payment_status,
                              categories=tuple(sorted({i["category"] for i in o.items})))

    def merchant_exists(self, merchant_id: str) -> bool:
        with self.lab.db.transaction() as s:
            return s.get(Merchant, merchant_id) is not None

    def merchant_name(self, merchant_id: str) -> str | None:
        with self.lab.db.transaction() as s:
            m = s.get(Merchant, merchant_id)
            return m.name if m else None

    def order_summary(self, order_id: str) -> dict | None:
        try:
            return self.lab.get_order(order_id)
        except NotFound:
            return None

    def set_payment_status(self, order_id: str, status: str, *, authorization_id: str | None = None,
                           provider_ref: str | None = None) -> None:
        """Move an order's payment status. Called only by PACT.

        * Transitions follow ``PAYMENT_TRANSITIONS``; anything else raises ``InvalidTransition``
          (``INVALID_PAYMENT_TRANSITION``). Re-setting the current status is an idempotent no-op that
          may fill in a missing reference.
        * While an attempt is in flight (PENDING/APPROVED) the order is bound to its authorization;
          a different ``authorization_id`` raises ``AUTHORIZATION_MISMATCH``.
        * ``CAPTURED`` needs a provider reference (the verified capture id), is terminal, and
          decrements stock exactly once.
        * A retry (FAILED/CANCELLED -> PENDING) drops the previous attempt's provider reference.
        """
        if status not in PAYMENT_STATUSES:
            raise PactError("INVALID_PAYMENT_STATUS", f"Unknown payment status {status!r}")
        with self.lab.db.transaction() as s:
            o = s.get(Order, order_id, with_for_update=True)
            if not o:
                raise NotFound("ORDER_NOT_FOUND", f"No order {order_id}")
            current = o.payment_status

            if current == status:
                if status == "CAPTURED":
                    if provider_ref and provider_ref != o.provider_ref:
                        raise Conflict("PROVIDER_REF_MISMATCH", "Order is already captured with another reference")
                    return
                self._bind_authorization(o, authorization_id)
                if provider_ref:
                    o.provider_ref = provider_ref
                o.updated_at = utcnow()
                return

            if status not in PAYMENT_TRANSITIONS[current]:
                raise InvalidTransition("INVALID_PAYMENT_TRANSITION",
                                        f"Payment status cannot change from {current} to {status}",
                                        details={"from": current, "to": status})

            if current in IN_FLIGHT_STATUSES:
                self._bind_authorization(o, authorization_id)
            elif authorization_id:
                o.authorization_id = authorization_id  # a new attempt starts (CREATED/FAILED/CANCELLED -> PENDING)
            if status == "PENDING" and current in {"FAILED", "CANCELLED"}:
                o.provider_ref = None  # the previous attempt's provider order is no longer relevant
            if provider_ref:
                o.provider_ref = provider_ref
            if status == "CAPTURED":
                if not o.provider_ref:
                    raise PactError("PROVIDER_REF_REQUIRED", "CAPTURED requires the verified provider capture reference")
                for line in o.items:
                    p = s.get(Product, line["product_id"], with_for_update=True)
                    if p:
                        # The payment has already been captured; never block it on stock. Clamp at zero.
                        p.stock = max(p.stock - line["quantity"], 0)
            o.payment_status = status
            o.updated_at = utcnow()

    @staticmethod
    def _bind_authorization(o: Order, authorization_id: str | None) -> None:
        if not authorization_id:
            return
        if o.authorization_id and o.authorization_id != authorization_id:
            raise Conflict("AUTHORIZATION_MISMATCH",
                           "Order is bound to a different authorization for the payment in flight",
                           details={"order_id": o.id})
        o.authorization_id = authorization_id
