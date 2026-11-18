from uuid import uuid4

import pytest

from conftest import B2C, actor
from neomarket.api import fail
from neomarket.b2c import B2BUnavailable, LocalB2B
from neomarket.main import retry_pending
from test_b2b import decision, quantities


def test_add_sku_increments_quantity_if_already_in_cart(market):
    _, sku = market.listing()
    market.add_cart(sku, 2)
    cart = market.add_cart(sku, 3)
    assert len(cart["items"]) == 1 and cart["items"][0]["quantity"] == 5


def test_get_cart_enriched_with_b2b_data(market):
    product, sku = market.listing()
    market.add_cart(sku, 2)
    [item] = market.call("GET", B2C + "/cart", market.buyer)["items"]
    assert item["product_id"] == product["id"] and item["name"] == sku["name"]
    assert item["available_quantity"] == 5 and item["line_total"] == 1800
    assert item["image"]["url"] == sku["images"][0]["url"]


def test_unavailable_sku_shown_with_reason(market):
    product, sku = market.listing()
    market.add_cart(sku)
    decision(market, product)
    [item] = market.call("GET", B2C + "/cart", market.buyer)["items"]
    assert not item["is_available"] and item["unavailable_reason"] == "PRODUCT_BLOCKED"


def test_guest_cart_merged_on_login(market):
    _, sku = market.listing()
    guest = {"X-Session-Id": str(uuid4())}
    market.add_cart(sku, 2, guest)
    market.add_cart(sku, 1)
    merged = market.call("POST", B2C + "/cart/merge", market.buyer | guest)
    assert merged["items_count"] == 2
    assert market.call("GET", B2C + "/cart", guest)["items"] == []
    assert market.call("POST", B2C + "/cart/merge", market.buyer | guest)["items_count"] == 2


def test_catalog_returns_filtered_sorted_products(market):
    product = market.product(characteristics=[{"name": "color", "value": "red"}])
    market.sku(product, price=200)
    market.approve(market.claim())
    expensive, _ = market.listing(characteristics=[{"name": "color", "value": "red"}])
    market.listing(characteristics=[{"name": "color", "value": "blue"}])
    data = market.call("GET", B2C + "/catalog/products",
                       params={"sort": "price_asc", "filter[attributes][color]": "red"})
    assert [item["id"] for item in data["items"]] == [product["id"], expensive["id"]]


def test_facets_return_counts_per_filter_value(market):
    for color in ("red", "blue", "red"):
        market.listing(characteristics=[{"name": "color", "value": color}])
    [facet] = market.call("GET", B2C + "/catalog/facets")["facets"]
    assert facet["name"] == "color"
    assert {item["value"]: item["count"] for item in facet["values"]} == {"red": 2, "blue": 1}


def test_invalid_sort_returns_400(market):
    market.call("GET", B2C + "/catalog/products?sort=unknown", status=400)


def unavailable(*args, **kwargs):
    raise B2BUnavailable()


def b2c_app(market):
    return next(route.app for route in market.app.routes if route.path == "/b2c")


def test_b2b_unavailable_returns_502(market, monkeypatch):
    monkeypatch.setattr(b2c_app(market).state.b2b, "catalog", unavailable)
    market.call("GET", B2C + "/catalog/products", status=502)


def test_product_card_returns_full_data_with_skus(market):
    product, sku = market.listing(description="Listing details")
    view = market.call("GET", B2C + f"/catalog/products/{product['id']}")
    assert view["description"] == "Listing details"
    assert view["skus"][0]["id"] == sku["id"]


def test_cost_price_absent_in_response(market):
    product, _ = market.listing()
    response = market.client.get(B2C + f"/catalog/products/{product['id']}")
    assert response.status_code == 200 and "cost_price" not in response.text


def test_blocked_product_returns_404(market):
    product, _ = market.listing()
    decision(market, product)
    market.call("GET", B2C + f"/catalog/products/{product['id']}", status=404)


def test_sku_without_stock_is_shown_as_unavailable(market):
    product, _ = market.listing()
    unavailable_sku = market.sku(product, stock=0)
    market.approve(market.claim())
    view = market.call("GET", B2C + f"/catalog/products/{product['id']}")
    variant = next(sku for sku in view["skus"] if sku["id"] == unavailable_sku["id"])
    assert not variant["is_available"] and variant["available_quantity"] == 0


def order_setup(market):
    _, sku = market.listing()
    market.add_cart(sku, 2)
    return sku, market.checkout_details()


def test_checkout_creates_paid_order_with_fixed_prices(market):
    sku, details = order_setup(market)
    order = market.checkout(details)
    assert order["status"] == "PAID" and order["total"] == 1800
    with market.app.state.database.transaction() as db:
        db.save("skus", sku["id"], dict(db.get("skus", sku["id"]), price=1000))
    view = market.call("GET", B2C + f"/orders/{order['id']}", market.buyer)
    assert view["items"][0]["unit_price"] == 900
    assert market.call("GET", B2C + "/cart", market.buyer)["items"] == []
    assert quantities(market, sku) == (3, 2)


def test_partial_reserve_failure_returns_409(market, monkeypatch):
    sku, details = order_setup(market)

    def reserve_fails(db, order_id, key, items):
        LocalB2B.reserve(db, order_id, key, items)
        fail(409, "INSUFFICIENT_STOCK", "Simulated second SKU failure")

    monkeypatch.setattr(b2c_app(market).state.b2b, "reserve", reserve_fails)
    error = market.checkout(details, status=409)
    assert error["code"] == "RESERVE_FAILED"
    assert quantities(market, sku) == (5, 0)
    assert market.records("orders") == [] and market.records("reservations") == []


def test_idempotency_returns_existing_order(market):
    sku, details = order_setup(market)
    first = market.checkout(details)
    assert market.checkout(details) == first
    assert quantities(market, sku) == (3, 2)


def test_checkout_rejects_changed_payload_for_same_key(market):
    sku, details = order_setup(market)
    market.checkout(details)
    error = market.checkout(details | {"comment": "Changed"}, status=409)
    assert error["code"] == "IDEMPOTENCY_CONFLICT"
    assert quantities(market, sku) == (3, 2)


def test_b2b_unavailable_returns_503(market, monkeypatch):
    sku, details = order_setup(market)
    monkeypatch.setattr(b2c_app(market).state.b2b, "reserve", unavailable)
    market.checkout(details, status=503)
    assert quantities(market, sku) == (5, 0)
    assert market.call("GET", B2C + "/cart", market.buyer)["items_count"] == 2


def order_to_cancel(market):
    sku, details = order_setup(market)
    order = market.checkout(details)
    return sku, order, B2C + f"/orders/{order['id']}/cancel"


def test_cancel_paid_order_transitions_to_cancelled(market):
    sku, _, path = order_to_cancel(market)
    result = market.call("POST", path, market.buyer, json={"reason": "Changed mind"})
    assert result["status"] == "CANCELLED" and result["cancel_reason"] == "Changed mind"
    market.call("POST", path, market.buyer, 409)
    assert quantities(market, sku) == (5, 0)


def test_unreserve_failure_transitions_to_cancel_pending(market, monkeypatch):
    sku, order, path = order_to_cancel(market)
    gateway = b2c_app(market).state.b2b
    with monkeypatch.context() as patch:
        patch.setattr(gateway, "unreserve", unavailable)
        assert market.call("POST", path, market.buyer)["status"] == "CANCEL_PENDING"
        assert quantities(market, sku) == (3, 2)
    retry_pending(market.app.state.database, gateway)
    assert market.call("GET", B2C + f"/orders/{order['id']}", market.buyer)["status"] == "CANCELLED"
    assert quantities(market, sku) == (5, 0)


@pytest.mark.parametrize("status", ["ASSEMBLING", "DELIVERING"])
def test_cancel_in_progress_releases_stock(market, status):
    sku, order, path = order_to_cancel(market)
    with market.app.state.database.transaction() as db:
        db.save("orders", order["id"], dict(order, status=status))
    assert market.call("POST", path, market.buyer)["status"] == "CANCELLED"
    assert quantities(market, sku) == (5, 0)


def test_cancel_delivered_order_returns_409(market):
    sku, order, path = order_to_cancel(market)
    with market.app.state.database.transaction() as db:
        db.save("orders", order["id"], dict(order, status="DELIVERED"))
    error = market.call("POST", path, market.buyer, 409)
    assert (error["code"], error["details"]["current_status"]) == ("CANCEL_NOT_ALLOWED", "DELIVERED")
    assert quantities(market, sku) == (3, 2)


def test_other_user_order_returns_404(market):
    _, order, path = order_to_cancel(market)
    other = actor("buyer")[1]
    market.call("GET", B2C + f"/orders/{order['id']}", other, 404)
    market.call("POST", path, other, 404)
