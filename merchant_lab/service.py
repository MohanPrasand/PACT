"""Merchant Lab: product catalogue, cart and orders. It creates commercial orders; it never authorises
or executes payments — that is PACT's job."""

from __future__ import annotations

from sqlalchemy import or_, select

from merchant_lab.models import CartItem, Merchant, Order, Product
from pact_core.crypto import new_id
from pact_core.db import Database, utcnow
from pact_core.errors import Conflict, NotFound, PactError
from pact_core.policy.engine import OrderFacts

PAYMENT_STATUSES = {"CREATED", "PENDING", "APPROVED", "CAPTURED", "FAILED", "CANCELLED"}
MUTABLE_STATUSES = {"CREATED", "FAILED", "CANCELLED"}


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


class MerchantLab:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------- catalogue
    def search_products(self, query: str | None = None, category: str | None = None,
                        max_price_minor: int | None = None, merchant_id: str | None = None) -> list[dict]:
        with self.db.transaction() as s:
            stmt = select(Product)
            if query:
                for word in query.lower().split():
                    like = f"%{word}%"
                    stmt = stmt.where(or_(Product.name.ilike(like), Product.description.ilike(like),
                                          Product.category.ilike(like)))
            if category:
                stmt = stmt.where(Product.category == category)
            if max_price_minor is not None:
                stmt = stmt.where(Product.price_minor <= max_price_minor)
            if merchant_id:
                stmt = stmt.where(Product.merchant_id == merchant_id)
            return [product_dict(p) for p in s.execute(stmt.order_by(Product.price_minor)).scalars()]

    def get_product(self, product_id: str) -> dict:
        with self.db.transaction() as s:
            p = s.get(Product, product_id)
            if not p:
                raise NotFound("PRODUCT_NOT_FOUND", f"No product {product_id}")
            return product_dict(p)

    def list_merchants(self) -> list[dict]:
        with self.db.transaction() as s:
            return [{"merchant_id": m.id, "name": m.name, "description": m.description}
                    for m in s.execute(select(Merchant)).scalars()]

    # ------------------------------------------------------------- cart
    def cart_add(self, cart_id: str, product_id: str, quantity: int = 1) -> dict:
        if quantity < 1:
            raise PactError("INVALID_QUANTITY", "Quantity must be at least 1")
        with self.db.transaction() as s:
            if not s.get(Product, product_id):
                raise NotFound("PRODUCT_NOT_FOUND", f"No product {product_id}")
            item = s.execute(select(CartItem).where(CartItem.cart_id == cart_id, CartItem.product_id == product_id)).scalar_one_or_none()
            if item:
                item.quantity += quantity
            else:
                s.add(CartItem(cart_id=cart_id, product_id=product_id, quantity=quantity))
        return self.cart_view(cart_id)

    def cart_remove(self, cart_id: str, product_id: str) -> dict:
        with self.db.transaction() as s:
            for item in s.execute(select(CartItem).where(CartItem.cart_id == cart_id, CartItem.product_id == product_id)).scalars():
                s.delete(item)
        return self.cart_view(cart_id)

    def cart_view(self, cart_id: str) -> dict:
        with self.db.transaction() as s:
            lines = []
            for item in s.execute(select(CartItem).where(CartItem.cart_id == cart_id)).scalars():
                p = s.get(Product, item.product_id)
                lines.append({"product_id": p.id, "name": p.name, "merchant_id": p.merchant_id, "quantity": item.quantity,
                              "unit_price_minor": p.price_minor, "line_total_minor": p.price_minor * item.quantity,
                              "currency": p.currency})
            return {"cart_id": cart_id, "items": lines, "total_minor": sum(x["line_total_minor"] for x in lines)}

    def order_from_cart(self, cart_id: str, buyer_ref: str = "") -> dict:
        cart = self.cart_view(cart_id)
        if not cart["items"]:
            raise PactError("CART_EMPTY", "Cart is empty")
        order = self.create_order([{"product_id": i["product_id"], "quantity": i["quantity"]} for i in cart["items"]], buyer_ref)
        with self.db.transaction() as s:
            for item in s.execute(select(CartItem).where(CartItem.cart_id == cart_id)).scalars():
                s.delete(item)
        return order

    # ------------------------------------------------------------- orders
    def _price_items(self, s, items: list[dict]) -> tuple[str, list[dict], int, str]:
        if not items:
            raise PactError("EMPTY_ORDER", "An order needs at least one item")
        lines, merchant_ids, currencies = [], set(), set()
        for it in items:
            qty = int(it.get("quantity", 1))
            if qty < 1:
                raise PactError("INVALID_QUANTITY", "Quantity must be at least 1")
            p = s.get(Product, it["product_id"])
            if not p:
                raise NotFound("PRODUCT_NOT_FOUND", f"No product {it['product_id']}")
            if p.stock < qty:
                raise Conflict("OUT_OF_STOCK", f"{p.name} has only {p.stock} in stock")
            merchant_ids.add(p.merchant_id)
            currencies.add(p.currency)
            lines.append({"product_id": p.id, "name": p.name, "category": p.category, "quantity": qty,
                          "unit_price_minor": p.price_minor})
        if len(merchant_ids) != 1 or len(currencies) != 1:
            raise PactError("MIXED_ORDER", "All items in an order must come from one merchant in one currency")
        total = sum(line["unit_price_minor"] * line["quantity"] for line in lines)
        return merchant_ids.pop(), lines, total, currencies.pop()

    def create_order(self, items: list[dict], buyer_ref: str = "") -> dict:
        with self.db.transaction() as s:
            merchant_id, lines, total, currency = self._price_items(s, items)
            o = Order(id=new_id("ord"), merchant_id=merchant_id, buyer_ref=buyer_ref, items=lines,
                      total_minor=total, currency=currency, payment_status="CREATED")
            s.add(o)
            s.flush()
            return order_dict(o, s.get(Merchant, merchant_id).name)

    def update_order_items(self, order_id: str, items: list[dict]) -> dict:
        """Change an unpaid order. PACT detects this as transaction modification after approval."""
        with self.db.transaction() as s:
            o = s.get(Order, order_id)
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

    def list_transactions(self, merchant_id: str | None = None, buyer_ref: str | None = None, limit: int = 50) -> list[dict]:
        with self.db.transaction() as s:
            stmt = select(Order).order_by(Order.created_at.desc()).limit(limit)
            if merchant_id:
                stmt = stmt.where(Order.merchant_id == merchant_id)
            if buyer_ref:
                stmt = stmt.where(Order.buyer_ref == buyer_ref)
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
        assert status in PAYMENT_STATUSES
        with self.lab.db.transaction() as s:
            o = s.get(Order, order_id)
            if not o:
                return
            if o.payment_status == "CAPTURED":
                return  # terminal for the merchant
            o.payment_status = status
            o.updated_at = utcnow()
            if authorization_id:
                o.authorization_id = authorization_id
            if provider_ref:
                o.provider_ref = provider_ref
            if status == "CAPTURED":
                for line in o.items:
                    p = s.get(Product, line["product_id"])
                    if p:
                        p.stock = max(p.stock - line["quantity"], 0)
