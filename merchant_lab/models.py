"""Merchant Lab persistence. Lives in the same database file for the MVP but shares no tables with PACT."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from pact_core.db import Base, utcnow


class Merchant(Base):
    __tablename__ = "ml_merchants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")


class Product(Base):
    __tablename__ = "ml_products"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("ml_merchants.id"))
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(48))
    price_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    stock: Mapped[int] = mapped_column(Integer, default=0)
    rating: Mapped[float] = mapped_column(Float, default=0.0)


class CartItem(Base):
    __tablename__ = "ml_cart_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cart_id: Mapped[str] = mapped_column(String(64), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("ml_products.id"))
    quantity: Mapped[int] = mapped_column(Integer)


class Order(Base):
    __tablename__ = "ml_orders"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("ml_merchants.id"))
    buyer_ref: Mapped[str] = mapped_column(String(64), default="")
    items: Mapped[list] = mapped_column(JSON)  # [{product_id, name, category, quantity, unit_price_minor}]
    total_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    # CREATED | PENDING | APPROVED | CAPTURED | FAILED | CANCELLED
    payment_status: Mapped[str] = mapped_column(String(16), default="CREATED")
    authorization_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    provider_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
