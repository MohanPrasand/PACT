"""Merchant Lab: seed catalogue, search, cart, orders and payment status (issue #9)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from merchant_lab.models import Order, Product
from merchant_lab.seed import MERCHANTS, PRODUCTS, seed_catalogue
from merchant_lab.service import MerchantLab, MerchantLabOrderSource
from pact_core.errors import Conflict, InvalidTransition, NotFound, PactError
from pact_core.policy.engine import PAYABLE_ORDER_STATUSES, OrderFacts

POLICIES = Path(__file__).resolve().parent.parent / "specification" / "policies"

ATLAS, FORGE, TITAN = "prd_kb_atlas_tkl", "prd_kb_forge_75", "prd_kb_titan_rgb"
MOUSE, MONITOR, HEADPHONES, HUB = "prd_mouse_wireless", "prd_monitor_27_qhd", "prd_headphones_wireless", "prd_hub_usbc_7"
DS_RETAIL, DS_SENTIMENT = "prd_ds_retail_prices", "prd_ds_consumer_sentiment"
QD_KB = "prd_kb_quickdeals_basic"


@pytest.fixture
def lab(db) -> MerchantLab:
    seed_catalogue(db)
    return MerchantLab(db)


@pytest.fixture
def source(lab) -> MerchantLabOrderSource:
    return MerchantLabOrderSource(lab)


def stock(lab: MerchantLab, product_id: str) -> int:
    return lab.get_product(product_id)["stock"]


def set_stock(lab: MerchantLab, product_id: str, value: int) -> None:
    with lab.db.transaction() as s:
        s.get(Product, product_id).stock = value


def ids(products: list[dict]) -> list[str]:
    return [p["product_id"] for p in products]


# ============================================================================ seed


class TestSeed:
    def test_first_run_creates_everything(self, db):
        counts = seed_catalogue(db)
        assert counts == {"merchants_created": 3, "products_created": 10, "merchants_total": 3, "products_total": 10}

    def test_second_run_changes_nothing(self, db):
        seed_catalogue(db)
        lab = MerchantLab(db)
        before = (lab.list_merchants(), lab.search_products())
        counts = seed_catalogue(db)
        assert counts == {"merchants_created": 0, "products_created": 0, "merchants_total": 3, "products_total": 10}
        assert (lab.list_merchants(), lab.search_products()) == before

    def test_reseed_does_not_reset_stock(self, lab, db):
        set_stock(lab, ATLAS, 3)
        seed_catalogue(db)
        assert stock(lab, ATLAS) == 3

    def test_merchants(self, lab):
        merchants = {m["merchant_id"]: m["name"] for m in lab.list_merchants()}
        assert merchants == {"m_techmart": "TechMart", "m_datahub": "DataHub Research",
                             "m_quickdeals": "QuickDeals Outlet"}

    def test_all_prices_usd_integer_minor_units_and_in_stock(self, lab):
        for p in lab.search_products():
            assert p["currency"] == "USD"
            assert isinstance(p["price_minor"], int) and p["price_minor"] > 0
            assert p["stock"] > 0
            assert 0 <= p["rating"] <= 5

    def test_expected_prices(self, lab):
        prices = {p["product_id"]: p["price_minor"] for p in lab.search_products()}
        assert prices == {ATLAS: 7900, FORGE: 6400, TITAN: 13900, MOUSE: 2900, MONITOR: 18900,
                          HEADPHONES: 11900, HUB: 2400, DS_RETAIL: 4500, DS_SENTIMENT: 2500, QD_KB: 3900}

    def test_techmart_categories_match_merchant_policy(self, lab):
        policy = json.loads((POLICIES / "merchant.m_techmart.json").read_text())
        cats = {p["category"] for p in lab.search_products(merchant_id="m_techmart")}
        assert cats == set(policy["allowed_categories"]) == {"peripherals", "displays", "audio"}

    def test_datahub_categories_match_merchant_policy(self, lab):
        policy = json.loads((POLICIES / "merchant.m_datahub.json").read_text())
        products = lab.search_products(merchant_id="m_datahub")
        assert len(products) == 2
        assert {p["category"] for p in products} == set(policy["allowed_categories"]) == {"datasets"}

    def test_org_policy_approval_matches_demo_intent(self):
        org = json.loads((POLICIES / "org.json").read_text())
        assert "m_techmart" in org["approved_merchants"]
        assert "m_datahub" in org["approved_merchants"]
        assert "m_quickdeals" not in org["approved_merchants"]  # demonstrates MERCHANT_NOT_APPROVED

    def test_seed_data_is_self_consistent(self):
        merchant_ids = {m["id"] for m in MERCHANTS}
        assert all(p["merchant_id"] in merchant_ids for p in PRODUCTS)
        assert len({p["id"] for p in PRODUCTS}) == len(PRODUCTS)


# ============================================================================ catalogue


class TestSearch:
    def test_no_filters_returns_all_cheapest_first(self, lab):
        prices = [p["price_minor"] for p in lab.search_products()]
        assert len(prices) == 10
        assert prices == sorted(prices)

    def test_query_is_case_insensitive(self, lab):
        assert set(ids(lab.search_products("KEYBOARD"))) == {ATLAS, FORGE, TITAN, QD_KB}

    def test_multi_word_query_requires_every_word(self, lab):
        assert ids(lab.search_products("wireless mouse")) == [MOUSE]
        assert set(ids(lab.search_products("wireless"))) == {MOUSE, HEADPHONES}
        assert ids(lab.search_products("mechanical rgb")) == [TITAN]

    def test_query_matches_category_and_description(self, lab):
        assert set(ids(lab.search_products("datasets"))) == {DS_RETAIL, DS_SENTIMENT}
        assert ids(lab.search_products("noise cancellation")) == [HEADPHONES]

    def test_query_no_match(self, lab):
        assert lab.search_products("toaster") == []

    def test_like_wildcards_are_literal(self, lab):
        assert ids(lab.search_products("%")) == [FORGE]  # only the "75%" description, not everything
        assert lab.search_products("_") == []

    def test_category_filter(self, lab):
        assert ids(lab.search_products(category="displays")) == [MONITOR]
        assert ids(lab.search_products(category="Audio")) == [HEADPHONES]

    def test_max_price_filter_inclusive(self, lab):
        result = lab.search_products("keyboard", max_price_minor=7900)
        assert set(ids(result)) == {ATLAS, FORGE, QD_KB}
        assert TITAN not in ids(lab.search_products("keyboard", max_price_minor=10000))

    def test_merchant_filter(self, lab):
        assert ids(lab.search_products("keyboard", merchant_id="m_quickdeals")) == [QD_KB]
        assert set(ids(lab.search_products("keyboard", merchant_id="m_techmart"))) == {ATLAS, FORGE, TITAN}

    def test_best_keyboard_under_100_dollars(self, lab):
        best = lab.search_products("keyboard", max_price_minor=10000, sort="rating")
        assert ids(best) == [ATLAS, FORGE, QD_KB]
        assert best[0]["merchant_id"] == "m_techmart"

    def test_sort_options(self, lab):
        kb = "keyboard"
        assert ids(lab.search_products(kb, sort="price")) == [QD_KB, FORGE, ATLAS, TITAN]
        assert ids(lab.search_products(kb, sort="price_desc")) == [TITAN, ATLAS, FORGE, QD_KB]
        assert ids(lab.search_products(kb, sort="rating")) == [TITAN, ATLAS, FORGE, QD_KB]
        names = [p["name"] for p in lab.search_products(kb, sort="name")]
        assert names == sorted(names)

    def test_invalid_sort_rejected(self, lab):
        with pytest.raises(PactError) as e:
            lab.search_products(sort="popularity")
        assert e.value.code == "INVALID_SORT"

    @pytest.mark.parametrize("bad", [-1, "100", 1.5, True])
    def test_invalid_max_price_rejected(self, lab, bad):
        with pytest.raises(PactError) as e:
            lab.search_products(max_price_minor=bad)
        assert e.value.code == "INVALID_PRICE_FILTER"

    def test_in_stock_only(self, lab):
        set_stock(lab, ATLAS, 0)
        assert ATLAS in ids(lab.search_products("keyboard"))
        assert ATLAS not in ids(lab.search_products("keyboard", in_stock_only=True))

    def test_product_dict_shape(self, lab):
        p = lab.get_product(ATLAS)
        assert p == {"product_id": ATLAS, "merchant_id": "m_techmart", "name": "Atlas TKL Mechanical Keyboard",
                     "description": p["description"], "category": "peripherals", "price_minor": 7900,
                     "currency": "USD", "price": "79.00", "stock": 25, "rating": 4.6}

    def test_get_product_not_found(self, lab):
        with pytest.raises(NotFound) as e:
            lab.get_product("prd_nope")
        assert e.value.code == "PRODUCT_NOT_FOUND"
        assert e.value.status_code == 404


# ============================================================================ cart


class TestCart:
    def test_empty_cart_view(self, lab):
        assert lab.cart_view("c1") == {"cart_id": "c1", "items": [], "item_count": 0, "total_minor": 0,
                                       "currency": None}

    def test_add_and_totals(self, lab):
        lab.cart_add("c1", ATLAS)
        cart = lab.cart_add("c1", MOUSE, 2)
        assert [(i["product_id"], i["quantity"], i["line_total_minor"]) for i in cart["items"]] == [
            (ATLAS, 1, 7900), (MOUSE, 2, 5800)]
        assert cart["total_minor"] == 13700
        assert cart["item_count"] == 3
        assert cart["currency"] == "USD"

    def test_add_same_product_increments(self, lab):
        lab.cart_add("c1", ATLAS)
        cart = lab.cart_add("c1", ATLAS, 2)
        assert len(cart["items"]) == 1
        assert cart["items"][0]["quantity"] == 3
        assert cart["total_minor"] == 3 * 7900

    def test_carts_are_isolated(self, lab):
        lab.cart_add("c1", ATLAS)
        assert lab.cart_view("c2")["items"] == []

    def test_remove(self, lab):
        lab.cart_add("c1", ATLAS, 2)
        lab.cart_add("c1", MOUSE)
        cart = lab.cart_remove("c1", ATLAS)
        assert [i["product_id"] for i in cart["items"]] == [MOUSE]
        assert cart["total_minor"] == 2900

    def test_remove_absent_is_noop(self, lab):
        lab.cart_add("c1", MOUSE)
        assert lab.cart_remove("c1", ATLAS)["total_minor"] == 2900

    def test_add_unknown_product(self, lab):
        with pytest.raises(NotFound):
            lab.cart_add("c1", "prd_nope")

    @pytest.mark.parametrize("qty", [0, -1, 1.5, "2", True, None])
    def test_add_invalid_quantity(self, lab, qty):
        with pytest.raises(PactError) as e:
            lab.cart_add("c1", ATLAS, qty)
        assert e.value.code == "INVALID_QUANTITY"

    def test_add_beyond_stock_rejected(self, lab):
        set_stock(lab, ATLAS, 2)
        lab.cart_add("c1", ATLAS, 2)
        with pytest.raises(Conflict) as e:
            lab.cart_add("c1", ATLAS)
        assert e.value.code == "OUT_OF_STOCK"
        assert lab.cart_view("c1")["items"][0]["quantity"] == 2

    def test_order_from_cart_creates_order_and_clears_cart(self, lab):
        lab.cart_add("c1", ATLAS)
        lab.cart_add("c1", MOUSE, 2)
        order = lab.order_from_cart("c1", buyer_ref="u_demo")
        assert order["merchant_id"] == "m_techmart"
        assert order["total_minor"] == 7900 + 2 * 2900
        assert order["buyer_ref"] == "u_demo"
        assert order["payment_status"] == "CREATED"
        assert lab.cart_view("c1")["items"] == []
        assert lab.get_order(order["order_id"]) == order

    def test_order_from_empty_cart(self, lab):
        with pytest.raises(PactError) as e:
            lab.order_from_cart("c1")
        assert e.value.code == "CART_EMPTY"

    def test_failed_order_from_cart_keeps_cart(self, lab):
        lab.cart_add("c1", ATLAS)
        lab.cart_add("c1", QD_KB)
        with pytest.raises(PactError) as e:
            lab.order_from_cart("c1")
        assert e.value.code == "MIXED_ORDER"
        assert len(lab.cart_view("c1")["items"]) == 2
        assert lab.list_transactions() == []


# ============================================================================ orders


class TestCreateOrder:
    def test_priced_from_catalogue(self, lab):
        order = lab.create_order([{"product_id": ATLAS, "quantity": 2}, {"product_id": HUB, "quantity": 1}])
        assert order["total_minor"] == 2 * 7900 + 2400
        assert order["total"] == "182.00"
        assert order["currency"] == "USD"
        assert order["merchant_name"] == "TechMart"
        assert order["items"] == [
            {"product_id": ATLAS, "name": "Atlas TKL Mechanical Keyboard", "category": "peripherals",
             "quantity": 2, "unit_price_minor": 7900},
            {"product_id": HUB, "name": "Nexus 7-Port USB-C Hub", "category": "peripherals",
             "quantity": 1, "unit_price_minor": 2400},
        ]

    @pytest.mark.parametrize("field", ["unit_price_minor", "price_minor", "price", "total_minor", "currency"])
    def test_client_price_fields_rejected(self, lab, field):
        with pytest.raises(PactError) as e:
            lab.create_order([{"product_id": ATLAS, "quantity": 1, field: 1}])
        assert e.value.code == "INVALID_ITEM"
        assert lab.list_transactions() == []

    def test_quantity_defaults_to_one(self, lab):
        assert lab.create_order([{"product_id": FORGE}])["total_minor"] == 6400

    def test_duplicate_lines_are_merged(self, lab):
        order = lab.create_order([{"product_id": MOUSE, "quantity": 1}, {"product_id": MOUSE, "quantity": 2}])
        assert order["items"] == [{"product_id": MOUSE, "name": "Glide Wireless Mouse", "category": "peripherals",
                                   "quantity": 3, "unit_price_minor": 2900}]
        assert order["total_minor"] == 8700

    def test_duplicate_lines_checked_against_stock_together(self, lab):
        set_stock(lab, MOUSE, 2)
        with pytest.raises(Conflict) as e:
            lab.create_order([{"product_id": MOUSE, "quantity": 1}, {"product_id": MOUSE, "quantity": 2}])
        assert e.value.code == "OUT_OF_STOCK"

    def test_mixed_merchant_rejected(self, lab):
        with pytest.raises(PactError) as e:
            lab.create_order([{"product_id": ATLAS, "quantity": 1}, {"product_id": DS_RETAIL, "quantity": 1}])
        assert e.value.code == "MIXED_ORDER"
        assert lab.list_transactions() == []

    def test_mixed_currency_rejected(self, lab):
        with lab.db.transaction() as s:
            s.add(Product(id="prd_eur_cable", merchant_id="m_techmart", name="Cable", category="peripherals",
                          price_minor=500, currency="EUR", stock=5))
        with pytest.raises(PactError) as e:
            lab.create_order([{"product_id": ATLAS}, {"product_id": "prd_eur_cable"}])
        assert e.value.code == "MIXED_ORDER"

    def test_out_of_stock_rejected(self, lab):
        set_stock(lab, MONITOR, 1)
        with pytest.raises(Conflict) as e:
            lab.create_order([{"product_id": MONITOR, "quantity": 2}])
        assert e.value.code == "OUT_OF_STOCK"
        assert e.value.details == {"product_id": MONITOR, "stock": 1, "requested": 2}

    @pytest.mark.parametrize("qty", [0, -3, 1.5, "1", True, None])
    def test_invalid_quantity_rejected(self, lab, qty):
        with pytest.raises(PactError) as e:
            lab.create_order([{"product_id": ATLAS, "quantity": qty}])
        assert e.value.code == "INVALID_QUANTITY"

    def test_unknown_product_rejected(self, lab):
        with pytest.raises(NotFound) as e:
            lab.create_order([{"product_id": "prd_nope", "quantity": 1}])
        assert e.value.code == "PRODUCT_NOT_FOUND"

    @pytest.mark.parametrize("items", [[], None, "abc"])
    def test_empty_order_rejected(self, lab, items):
        with pytest.raises(PactError) as e:
            lab.create_order(items)
        assert e.value.code == "EMPTY_ORDER"

    @pytest.mark.parametrize("item", ["prd_kb_atlas_tkl", {"quantity": 1}, {"product_id": 5}, {"product_id": ""}])
    def test_malformed_item_rejected(self, lab, item):
        with pytest.raises(PactError) as e:
            lab.create_order([item])
        assert e.value.code == "INVALID_ITEM"

    def test_creating_order_does_not_touch_stock(self, lab):
        lab.create_order([{"product_id": ATLAS, "quantity": 5}])
        assert stock(lab, ATLAS) == 25

    def test_order_ids_unguessable_and_unique(self, lab):
        a = lab.create_order([{"product_id": HUB}])["order_id"]
        b = lab.create_order([{"product_id": HUB}])["order_id"]
        assert a != b
        for oid in (a, b):
            prefix, token = oid.split("_")
            assert prefix == "ord" and len(token) == 16
            int(token, 16)

    def test_get_order_not_found(self, lab):
        with pytest.raises(NotFound) as e:
            lab.get_order("ord_missing")
        assert e.value.code == "ORDER_NOT_FOUND"


class TestUpdateOrderItems:
    def test_update_reprices_from_catalogue(self, lab, source):
        order = lab.create_order([{"product_id": FORGE}])
        updated = lab.update_order_items(order["order_id"], [{"product_id": TITAN, "quantity": 1}])
        assert updated["total_minor"] == 13900
        assert source.get_order(order["order_id"]).amount_minor == 13900

    @pytest.mark.parametrize("status_path", [[], ["PENDING", "FAILED"], ["PENDING", "CANCELLED"]])
    def test_allowed_while_unpaid(self, lab, source, status_path):
        oid = lab.create_order([{"product_id": FORGE}])["order_id"]
        for st in status_path:
            source.set_payment_status(oid, st, authorization_id="ar_1")
        assert lab.update_order_items(oid, [{"product_id": ATLAS}])["total_minor"] == 7900

    @pytest.mark.parametrize("status_path", [["PENDING"], ["PENDING", "APPROVED"], ["PENDING", "CAPTURED"]])
    def test_locked_once_payment_in_flight_or_captured(self, lab, source, status_path):
        oid = lab.create_order([{"product_id": FORGE}])["order_id"]
        for st in status_path:
            source.set_payment_status(oid, st, authorization_id="ar_1", provider_ref="cap_1")
        with pytest.raises(Conflict) as e:
            lab.update_order_items(oid, [{"product_id": ATLAS}])
        assert e.value.code == "ORDER_LOCKED"
        assert lab.get_order(oid)["total_minor"] == 6400

    def test_update_to_other_merchant_rejected(self, lab):
        oid = lab.create_order([{"product_id": FORGE}])["order_id"]
        with pytest.raises(PactError) as e:
            lab.update_order_items(oid, [{"product_id": DS_RETAIL}])
        assert e.value.code == "MIXED_ORDER"
        assert lab.get_order(oid)["merchant_id"] == "m_techmart"

    def test_update_rejects_client_prices(self, lab):
        oid = lab.create_order([{"product_id": FORGE}])["order_id"]
        with pytest.raises(PactError) as e:
            lab.update_order_items(oid, [{"product_id": TITAN, "unit_price_minor": 100}])
        assert e.value.code == "INVALID_ITEM"

    def test_update_unknown_order(self, lab):
        with pytest.raises(NotFound):
            lab.update_order_items("ord_missing", [{"product_id": FORGE}])


class TestListTransactions:
    def test_filters_and_ordering(self, lab, source):
        a = lab.create_order([{"product_id": FORGE}], buyer_ref="u_demo")["order_id"]
        b = lab.create_order([{"product_id": DS_RETAIL}], buyer_ref="u_demo")["order_id"]
        c = lab.create_order([{"product_id": HUB}], buyer_ref="u_other")["order_id"]
        source.set_payment_status(c, "PENDING")
        assert set(ids_of(lab.list_transactions())) == {a, b, c}
        assert set(ids_of(lab.list_transactions(merchant_id="m_techmart"))) == {a, c}
        assert ids_of(lab.list_transactions(merchant_id="m_datahub")) == [b]
        assert set(ids_of(lab.list_transactions(buyer_ref="u_demo"))) == {a, b}
        assert ids_of(lab.list_transactions(payment_status="PENDING")) == [c]
        assert ids_of(lab.list_transactions(merchant_id="m_techmart", buyer_ref="u_demo")) == [a]
        assert len(lab.list_transactions(limit=2)) == 2

    def test_newest_first(self, lab):
        first = lab.create_order([{"product_id": FORGE}])["order_id"]
        second = lab.create_order([{"product_id": HUB}])["order_id"]
        from datetime import datetime
        with lab.db.transaction() as s:
            s.get(Order, first).created_at = datetime(2026, 1, 1)
            s.get(Order, second).created_at = datetime(2026, 1, 2)
        assert ids_of(lab.list_transactions()) == [second, first]

    def test_merchant_name_included(self, lab):
        lab.create_order([{"product_id": DS_SENTIMENT}])
        assert lab.list_transactions()[0]["merchant_name"] == "DataHub Research"

    @pytest.mark.parametrize("limit", [0, -1, 501, "10", True])
    def test_invalid_limit(self, lab, limit):
        with pytest.raises(PactError) as e:
            lab.list_transactions(limit=limit)
        assert e.value.code == "INVALID_LIMIT"

    def test_invalid_status_filter(self, lab):
        with pytest.raises(PactError) as e:
            lab.list_transactions(payment_status="PAID")
        assert e.value.code == "INVALID_PAYMENT_STATUS"


def ids_of(orders: list[dict]) -> list[str]:
    return [o["order_id"] for o in orders]


# ============================================================================ OrderSource port


class TestOrderSource:
    def test_order_facts(self, lab, source):
        oid = lab.create_order([{"product_id": ATLAS}, {"product_id": HEADPHONES}, {"product_id": MONITOR}])["order_id"]
        facts = source.get_order(oid)
        assert isinstance(facts, OrderFacts)
        assert facts == OrderFacts(order_id=oid, merchant_id="m_techmart", amount_minor=7900 + 11900 + 18900,
                                   currency="USD", status="CREATED",
                                   categories=("audio", "displays", "peripherals"))

    def test_categories_deduplicated(self, lab, source):
        oid = lab.create_order([{"product_id": ATLAS}, {"product_id": MOUSE}])["order_id"]
        assert source.get_order(oid).categories == ("peripherals",)

    def test_unknown_order_is_none(self, source):
        assert source.get_order("ord_missing") is None
        assert source.order_summary("ord_missing") is None

    def test_order_summary(self, lab, source):
        order = lab.create_order([{"product_id": DS_RETAIL}])
        assert source.order_summary(order["order_id"]) == order

    def test_merchants(self, lab, source):
        assert source.merchant_exists("m_quickdeals")
        assert not source.merchant_exists("m_nope")
        assert source.merchant_name("m_datahub") == "DataHub Research"
        assert source.merchant_name("m_nope") is None

    def test_new_order_is_payable(self, lab, source):
        oid = lab.create_order([{"product_id": FORGE}])["order_id"]
        assert source.get_order(oid).status in PAYABLE_ORDER_STATUSES

    def test_satisfies_port(self, source):
        from pact_core.ports import OrderSource

        def needs_port(port: OrderSource) -> OrderSource:
            return port

        for name in ("get_order", "merchant_exists", "merchant_name", "order_summary", "set_payment_status"):
            assert callable(getattr(needs_port(source), name))


# ============================================================================ payment status


@pytest.fixture
def order_id(lab) -> str:
    return lab.create_order([{"product_id": ATLAS, "quantity": 2}, {"product_id": MOUSE, "quantity": 1}])["order_id"]


def status(lab: MerchantLab, oid: str) -> str:
    return lab.get_order(oid)["payment_status"]


class TestPaymentStatus:
    def test_happy_path_created_pending_captured(self, lab, source, order_id):
        source.set_payment_status(order_id, "PENDING", authorization_id="ar_1")
        source.set_payment_status(order_id, "CAPTURED", authorization_id="ar_1", provider_ref="cap_123")
        o = lab.get_order(order_id)
        assert (o["payment_status"], o["authorization_id"], o["provider_ref"]) == ("CAPTURED", "ar_1", "cap_123")
        assert source.get_order(order_id).status == "CAPTURED"
        assert "CAPTURED" not in PAYABLE_ORDER_STATUSES

    def test_via_approved(self, lab, source, order_id):
        source.set_payment_status(order_id, "PENDING", authorization_id="ar_1")
        source.set_payment_status(order_id, "APPROVED", provider_ref="pp_order_1")
        source.set_payment_status(order_id, "CAPTURED", provider_ref="cap_1")
        assert status(lab, order_id) == "CAPTURED"

    @pytest.mark.parametrize("outcome", ["FAILED", "CANCELLED"])
    def test_pending_to_failure_and_retry(self, lab, source, order_id, outcome):
        source.set_payment_status(order_id, "PENDING", authorization_id="ar_1", provider_ref="pp_1")
        source.set_payment_status(order_id, outcome)
        assert status(lab, order_id) == outcome
        assert source.get_order(order_id).status in PAYABLE_ORDER_STATUSES
        source.set_payment_status(order_id, "PENDING", authorization_id="ar_2")
        o = lab.get_order(order_id)
        assert (o["payment_status"], o["authorization_id"], o["provider_ref"]) == ("PENDING", "ar_2", None)
        source.set_payment_status(order_id, "CAPTURED", provider_ref="cap_2")
        assert status(lab, order_id) == "CAPTURED"

    @pytest.mark.parametrize("path,bad", [
        ([], "CAPTURED"),
        ([], "FAILED"),
        ([], "CANCELLED"),
        ([], "APPROVED"),
        (["PENDING"], "CREATED"),
        (["PENDING", "APPROVED"], "PENDING"),
        (["PENDING", "FAILED"], "CAPTURED"),
        (["PENDING", "FAILED"], "CANCELLED"),
        (["PENDING", "CANCELLED"], "CREATED"),
    ])
    def test_invalid_transitions_rejected(self, lab, source, order_id, path, bad):
        for st in path:
            source.set_payment_status(order_id, st)
        with pytest.raises(InvalidTransition) as e:
            source.set_payment_status(order_id, bad, provider_ref="cap_x")
        assert e.value.code == "INVALID_PAYMENT_TRANSITION"
        assert e.value.details == {"from": path[-1] if path else "CREATED", "to": bad}
        assert status(lab, order_id) == (path[-1] if path else "CREATED")

    @pytest.mark.parametrize("bad", ["CREATED", "PENDING", "APPROVED", "FAILED", "CANCELLED"])
    def test_captured_is_terminal(self, lab, source, order_id, bad):
        source.set_payment_status(order_id, "PENDING")
        source.set_payment_status(order_id, "CAPTURED", provider_ref="cap_1")
        with pytest.raises(InvalidTransition):
            source.set_payment_status(order_id, bad)
        assert status(lab, order_id) == "CAPTURED"

    @pytest.mark.parametrize("bad", ["PAID", "captured", "", "REFUNDED"])
    def test_unknown_status_rejected(self, lab, source, order_id, bad):
        with pytest.raises(PactError) as e:
            source.set_payment_status(order_id, bad)
        assert e.value.code == "INVALID_PAYMENT_STATUS"
        assert status(lab, order_id) == "CREATED"

    def test_unknown_order_rejected(self, source):
        with pytest.raises(NotFound) as e:
            source.set_payment_status("ord_missing", "PENDING")
        assert e.value.code == "ORDER_NOT_FOUND"

    def test_captured_requires_provider_ref(self, lab, source, order_id):
        source.set_payment_status(order_id, "PENDING", authorization_id="ar_1")
        with pytest.raises(PactError) as e:
            source.set_payment_status(order_id, "CAPTURED")
        assert e.value.code == "PROVIDER_REF_REQUIRED"
        assert status(lab, order_id) == "PENDING"
        assert stock(lab, ATLAS) == 25

    def test_captured_accepts_ref_set_earlier(self, lab, source, order_id):
        source.set_payment_status(order_id, "PENDING", provider_ref="pp_1")
        source.set_payment_status(order_id, "CAPTURED")
        assert lab.get_order(order_id)["provider_ref"] == "pp_1"

    def test_same_status_is_idempotent_and_fills_refs(self, lab, source, order_id):
        source.set_payment_status(order_id, "PENDING", authorization_id="ar_1")
        source.set_payment_status(order_id, "PENDING", authorization_id="ar_1", provider_ref="pp_1")
        source.set_payment_status(order_id, "PENDING")
        o = lab.get_order(order_id)
        assert (o["payment_status"], o["authorization_id"], o["provider_ref"]) == ("PENDING", "ar_1", "pp_1")

    def test_in_flight_authorization_mismatch_rejected(self, lab, source, order_id):
        source.set_payment_status(order_id, "PENDING", authorization_id="ar_1")
        with pytest.raises(Conflict) as e:
            source.set_payment_status(order_id, "CAPTURED", authorization_id="ar_other", provider_ref="cap_1")
        assert e.value.code == "AUTHORIZATION_MISMATCH"
        with pytest.raises(Conflict):
            source.set_payment_status(order_id, "PENDING", authorization_id="ar_other")
        o = lab.get_order(order_id)
        assert (o["payment_status"], o["authorization_id"]) == ("PENDING", "ar_1")

    def test_recapture_with_different_ref_rejected(self, lab, source, order_id):
        source.set_payment_status(order_id, "PENDING")
        source.set_payment_status(order_id, "CAPTURED", provider_ref="cap_1")
        source.set_payment_status(order_id, "CAPTURED", provider_ref="cap_1")  # idempotent
        with pytest.raises(Conflict) as e:
            source.set_payment_status(order_id, "CAPTURED", provider_ref="cap_2")
        assert e.value.code == "PROVIDER_REF_MISMATCH"
        assert lab.get_order(order_id)["provider_ref"] == "cap_1"

    def test_updated_at_moves(self, lab, source, order_id):
        before = lab.get_order(order_id)["updated_at"]
        source.set_payment_status(order_id, "PENDING")
        assert lab.get_order(order_id)["updated_at"] >= before


class TestStockOnCapture:
    def test_stock_decremented_exactly_once(self, lab, source, order_id):
        source.set_payment_status(order_id, "PENDING")
        source.set_payment_status(order_id, "CAPTURED", provider_ref="cap_1")
        assert (stock(lab, ATLAS), stock(lab, MOUSE)) == (23, 59)
        source.set_payment_status(order_id, "CAPTURED", provider_ref="cap_1")
        source.set_payment_status(order_id, "CAPTURED")
        assert (stock(lab, ATLAS), stock(lab, MOUSE)) == (23, 59)

    @pytest.mark.parametrize("path", [["PENDING"], ["PENDING", "APPROVED"], ["PENDING", "FAILED"],
                                      ["PENDING", "CANCELLED"], ["PENDING", "FAILED", "PENDING"]])
    def test_no_decrement_before_capture(self, lab, source, order_id, path):
        for st in path:
            source.set_payment_status(order_id, st)
        assert (stock(lab, ATLAS), stock(lab, MOUSE)) == (25, 60)

    def test_capture_never_drives_stock_negative(self, lab, source, order_id):
        set_stock(lab, ATLAS, 1)  # sold elsewhere after the order was created
        source.set_payment_status(order_id, "PENDING")
        source.set_payment_status(order_id, "CAPTURED", provider_ref="cap_1")
        assert stock(lab, ATLAS) == 0
        assert status(lab, order_id) == "CAPTURED"

    def test_capture_after_retry_decrements_once(self, lab, source, order_id):
        source.set_payment_status(order_id, "PENDING", authorization_id="ar_1")
        source.set_payment_status(order_id, "FAILED")
        source.set_payment_status(order_id, "PENDING", authorization_id="ar_2")
        source.set_payment_status(order_id, "CAPTURED", provider_ref="cap_2")
        assert stock(lab, ATLAS) == 23

    def test_only_captured_order_lines_affected(self, lab, source, order_id):
        other = lab.create_order([{"product_id": HUB, "quantity": 4}])["order_id"]
        source.set_payment_status(order_id, "PENDING")
        source.set_payment_status(order_id, "CAPTURED", provider_ref="cap_1")
        assert stock(lab, HUB) == 80
        assert status(lab, other) == "CREATED"


def test_orders_table_holds_server_prices(lab, db):
    """The persisted order rows carry catalogue prices, whatever the caller tried."""
    lab.create_order([{"product_id": TITAN, "quantity": 1}])
    with db.transaction() as s:
        o = s.execute(select(Order)).scalar_one()
        assert o.items[0]["unit_price_minor"] == 13900
        assert o.total_minor == 13900
