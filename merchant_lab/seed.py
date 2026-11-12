"""Demo catalogue for Merchant Lab.

Merchant ids and product categories line up with the PACT policy documents in
``specification/policies/``:

* ``m_techmart`` is on the org approved list; its merchant policy allows ``peripherals``,
  ``displays`` and ``audio``.
* ``m_datahub`` is approved; it sells ``datasets`` to the research agent only.
* ``m_quickdeals`` is deliberately **not** approved, to demonstrate ``MERCHANT_NOT_APPROVED``.

Whether a merchant is approved is PACT policy, not Merchant Lab data, so nothing here marks a
merchant as approved or not. All names are generic; prices are USD minor units (cents).
"""

from __future__ import annotations

from sqlalchemy import func, select

from merchant_lab.models import Merchant, Product
from pact_core.db import Database

MERCHANTS: tuple[dict, ...] = (
    {"id": "m_techmart", "name": "TechMart",
     "description": "Computer peripherals, displays and audio gear."},
    {"id": "m_datahub", "name": "DataHub Research",
     "description": "Licensed research datasets for analysis and modelling."},
    {"id": "m_quickdeals", "name": "QuickDeals Outlet",
     "description": "Discount outlet for unbranded electronics. Not on the organisation's approved list."},
)

PRODUCTS: tuple[dict, ...] = (
    # --- TechMart: three keyboards so "find the best keyboard under $100" has a real answer.
    {"id": "prd_kb_atlas_tkl", "merchant_id": "m_techmart", "name": "Atlas TKL Mechanical Keyboard",
     "description": "Tenkeyless mechanical keyboard with hot-swappable tactile switches and PBT keycaps.",
     "category": "peripherals", "price_minor": 7900, "stock": 25, "rating": 4.6},
    {"id": "prd_kb_forge_75", "merchant_id": "m_techmart", "name": "Forge 75 Mechanical Keyboard",
     "description": "Compact 75% mechanical keyboard with linear switches and a volume knob.",
     "category": "peripherals", "price_minor": 6400, "stock": 40, "rating": 4.3},
    {"id": "prd_kb_titan_rgb", "merchant_id": "m_techmart", "name": "Titan Full-Size RGB Mechanical Keyboard",
     "description": "Full-size mechanical keyboard with per-key RGB lighting, media keys and wrist rest.",
     "category": "peripherals", "price_minor": 13900, "stock": 15, "rating": 4.8},
    {"id": "prd_mouse_wireless", "merchant_id": "m_techmart", "name": "Glide Wireless Mouse",
     "description": "Ergonomic wireless mouse with silent clicks and a USB-C rechargeable battery.",
     "category": "peripherals", "price_minor": 2900, "stock": 60, "rating": 4.4},
    {"id": "prd_monitor_27_qhd", "merchant_id": "m_techmart", "name": "Vista 27\" 1440p Monitor",
     "description": "27-inch 2560x1440 IPS monitor, 144 Hz, height-adjustable stand.",
     "category": "displays", "price_minor": 18900, "stock": 10, "rating": 4.5},
    {"id": "prd_headphones_wireless", "merchant_id": "m_techmart", "name": "Echo Wireless Headphones",
     "description": "Over-ear wireless headphones with active noise cancellation and 30-hour battery.",
     "category": "audio", "price_minor": 11900, "stock": 20, "rating": 4.2},
    {"id": "prd_hub_usbc_7", "merchant_id": "m_techmart", "name": "Nexus 7-Port USB-C Hub",
     "description": "7-port USB-C hub with HDMI, SD card reader and 100 W pass-through charging.",
     "category": "peripherals", "price_minor": 2400, "stock": 80, "rating": 4.1},
    # --- DataHub: research datasets (research agent only, per merchant policy).
    {"id": "prd_ds_retail_prices", "merchant_id": "m_datahub", "name": "Retail Price Index Dataset 2026",
     "description": "Monthly retail price observations across 40 product categories, CSV and Parquet.",
     "category": "datasets", "price_minor": 4500, "stock": 1000, "rating": 4.7},
    {"id": "prd_ds_consumer_sentiment", "merchant_id": "m_datahub", "name": "Consumer Sentiment Survey Dataset",
     "description": "Anonymised quarterly consumer sentiment survey responses with demographic weights.",
     "category": "datasets", "price_minor": 2500, "stock": 1000, "rating": 4.4},
    # --- QuickDeals: not approved; a cheap keyboard that tempts a price-only search.
    {"id": "prd_kb_quickdeals_basic", "merchant_id": "m_quickdeals", "name": "Unbranded Mechanical Keyboard",
     "description": "Budget mechanical keyboard with blue switches. Generic packaging.",
     "category": "peripherals", "price_minor": 3900, "stock": 100, "rating": 3.9},
)

CURRENCY = "USD"


def seed_catalogue(db: Database) -> dict:
    """Insert the demo merchants and products that are missing. Idempotent.

    Existing rows are never modified, so re-seeding does not reset stock that captures have
    decremented. Returns how many rows were created and how many exist afterwards."""
    created_merchants = created_products = 0
    with db.transaction() as s:
        for m in MERCHANTS:
            if s.get(Merchant, m["id"]) is None:
                s.add(Merchant(**m))
                created_merchants += 1
        s.flush()
        for p in PRODUCTS:
            if s.get(Product, p["id"]) is None:
                s.add(Product(currency=CURRENCY, **p))
                created_products += 1
        s.flush()
        merchants_total = s.scalar(select(func.count()).select_from(Merchant))
        products_total = s.scalar(select(func.count()).select_from(Product))
    return {
        "merchants_created": created_merchants,
        "products_created": created_products,
        "merchants_total": merchants_total,
        "products_total": products_total,
    }
