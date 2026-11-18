from uuid import uuid4

from conftest import B2B, SERVICE, actor
from neomarket.main import DEFAULT_CATEGORY, HARD_REASON, SOFT_REASON
from neomarket.storage import timestamp


def test_create_product_returns_201_with_created_status(market):
    product = market.product()
    assert product["status"] == "CREATED"
    assert product["skus"] == []
    assert market.records("tickets") == []


def test_seller_id_taken_from_jwt(market):
    assert market.product(seller_id=str(uuid4()))["seller_id"] == market.seller_id


def test_missing_images_returns_400(market):
    result = market.call("POST", B2B + "/products", market.seller, 400,
                         json={"title": "A", "description": "B", "category_id": DEFAULT_CATEGORY})
    assert result["code"] == "IMAGES_REQUIRED"


def test_missing_category_returns_400(market):
    result = market.call("POST", B2B + "/products", market.seller, 400, json={"title": "A", "description": "B"})
    assert result["code"] == "INVALID_CATEGORY"


def test_invalid_category_id_returns_400(market):
    market.call("POST", B2B + "/products", market.seller, 400,
                json={"title": "A", "description": "B", "category_id": str(uuid4())})


def test_first_sku_transitions_product_to_on_moderation(market):
    product = market.product()
    market.sku(product)
    assert market.get_product(product)["status"] == "ON_MODERATION"


def test_first_sku_emits_created_event_to_moderation(market):
    product = market.product()
    sku = market.sku(product)
    [event] = market.records("moderation_inbox")
    assert event["event_type"] == "PRODUCT_CREATED"
    assert event["payload"]["json_after"]["skus"][0]["id"] == sku["id"]
    assert "cost_price" not in event["payload"]["json_after"]["skus"][0]


def test_second_sku_no_state_change(market):
    product = market.product()
    market.sku(product)
    market.sku(product)
    assert market.get_product(product)["status"] == "ON_MODERATION"
    assert len(market.records("moderation_inbox")) == 1


def test_add_sku_to_moderated_product_requires_another_review(market):
    product, first = market.listing()
    second = market.sku(product)
    assert market.get_product(product)["status"] == "ON_MODERATION"
    [edited] = [e for e in market.records("moderation_inbox") if e["event_type"] == "PRODUCT_EDITED"]
    assert [sku["id"] for sku in edited["payload"]["json_before"]["skus"]] == [first["id"]]
    assert [sku["id"] for sku in edited["payload"]["json_after"]["skus"]] == [first["id"], second["id"]]
    assert market.call("GET", B2B + "/public/products", SERVICE)["items"] == []


def test_add_sku_to_hard_blocked_returns_403(market):
    product, _ = market.listing()
    decision(market, product, hard=True)
    market.call("POST", B2B + "/skus", market.seller, 403,
                json={"product_id": product["id"], "name": "Variant", "price": 0})


def test_missing_image_returns_400(market):
    product = market.product()
    market.call("POST", B2B + "/skus", market.seller, 400,
                json={"product_id": product["id"], "name": "Variant", "price": 0})


def decision(market, product, *, hard=False, approve=False, key=None):
    report = {"field_name": "title", "comment": "Update title"}
    data = {"product_id": product["id"], "event_type": "MODERATED" if approve else "BLOCKED",
            "hard_block": hard, "idempotency_key": key or str(uuid4()), "occurred_at": timestamp(),
            "blocking_reason_id": HARD_REASON if hard else SOFT_REASON,
            "moderator_comment": "Review complete", "field_reports": [report]}
    market.call("POST", B2B + "/moderation/events", SERVICE, 204, json=data)
    return data


def test_get_moderated_product_returns_full_payload(market):
    product, sku = market.listing()
    view = market.get_product(product)
    assert view["status"] == "MODERATED"
    assert view["skus"][0]["id"] == sku["id"]
    assert view["skus"][0]["cost_price"] == 400
    assert not view["blocked"]


def test_get_blocked_product_returns_blocking_reason_and_field_reports(market):
    product, _ = market.listing()
    decision(market, product)
    view = market.get_product(product)
    assert view["blocking_reason"]["id"] == SOFT_REASON
    assert view["field_reports"][0]["field_name"] == "title"


def test_get_others_product_returns_404(market):
    market.get_product(market.product(), 404, actor("seller")[1])


def test_get_nonexistent_returns_404(market):
    market.get_product({"id": str(uuid4())}, 404)


def test_catalog_returns_moderated_in_stock_products(market):
    product, _ = market.listing()
    market.listing(stock=0)
    market.sku(market.product())
    data = market.call("GET", B2B + "/public/products", SERVICE)
    assert [item["id"] for item in data["items"]] == [product["id"]]


def test_catalog_excludes_hard_blocked(market):
    product, _ = market.listing()
    decision(market, product, hard=True)
    assert market.call("GET", B2B + "/public/products", SERVICE)["items"] == []


def test_catalog_missing_service_key_returns_401(market):
    market.call("GET", B2B + "/public/products", status=401)


def test_catalog_response_has_no_cost_price(market):
    product, _ = market.listing()
    data = market.call("GET", B2B + f"/public/products/{product['id']}", SERVICE)
    assert "cost_price" not in data["skus"][0]


def test_batch_ids_returns_visible_subset(market):
    product, _ = market.listing()
    hidden = market.product()
    data = market.call("POST", B2B + "/public/products/batch", SERVICE,
                       json={"product_ids": [hidden["id"], product["id"], str(uuid4())]})
    assert [item["id"] for item in data] == [product["id"]]


def stock_request(*sku_counts):
    return {"order_id": str(uuid4()), "idempotency_key": str(uuid4()), "items": [
        {"sku_id": sku["id"], "quantity": quantity} for sku, quantity in sku_counts
    ]}


def quantities(market, sku):
    return next((stored["active_quantity"], stored["reserved_quantity"])
                for stored in market.records("skus") if stored["id"] == sku["id"])


def test_reserve_all_skus_succeeds(market):
    product, first = market.listing()
    second = market.sku(product)
    market.approve(market.claim())
    request = stock_request((first, 2), (second, 3))
    assert market.call("POST", B2B + "/inventory/reserve", SERVICE, json=request)["status"] == "RESERVED"
    assert quantities(market, first) == (3, 2)
    assert quantities(market, second) == (2, 3)


def test_partial_insufficient_stock_returns_409_all_rollback(market):
    product, first = market.listing()
    second = market.sku(product)
    market.approve(market.claim())
    request = stock_request((first, 2), (second, 6))
    error = market.call("POST", B2B + "/inventory/reserve", SERVICE, 409, json=request)
    assert error["details"]["failed_items"] == [{"sku_id": second["id"], "requested": 6, "available": 5}]
    assert quantities(market, first) == quantities(market, second) == (5, 0)
    assert market.records("reservations") == []


def test_idempotent_reserve_returns_200_without_double_deduction(market):
    _, sku = market.listing()
    request = stock_request((sku, 2))
    first = market.call("POST", B2B + "/inventory/reserve", SERVICE, json=request)
    assert market.call("POST", B2B + "/inventory/reserve", SERVICE, json=request) == first
    assert quantities(market, sku) == (3, 2)


def test_sku_out_of_stock_event_emitted(market):
    _, sku = market.listing()
    market.call("POST", B2B + "/inventory/reserve", SERVICE, json=stock_request((sku, 5)))
    [event] = [event for event in market.records("events") if event["event_type"] == "SKU_OUT_OF_STOCK"]
    assert event["payload"]["sku_id"] == sku["id"]


def test_unreserve_restores_quantities(market):
    _, sku = market.listing()
    request = stock_request((sku, 3))
    market.call("POST", B2B + "/inventory/reserve", SERVICE, json=request)
    request.pop("idempotency_key")
    release = market.call("POST", B2B + "/inventory/unreserve", SERVICE, json=request)
    assert market.call("POST", B2B + "/inventory/unreserve", SERVICE, json=request) == release
    assert quantities(market, sku) == (5, 0)


def test_moderated_event_clears_blocking_data(market):
    product, _ = market.listing()
    decision(market, product)
    decision(market, product, approve=True)
    view = market.get_product(product)
    assert view["status"] == "MODERATED"
    assert view["blocking_reason"] is None and view["field_reports"] == []


def test_blocked_soft_saves_field_reports(market):
    product, _ = market.listing()
    event = decision(market, product)
    assert market.get_product(product)["field_reports"][0]["comment"] == event["field_reports"][0]["comment"]


def test_blocked_hard_sets_terminal_status(market):
    product, _ = market.listing()
    decision(market, product, hard=True)
    decision(market, product, approve=True)
    assert market.get_product(product)["status"] == "HARD_BLOCKED"


def test_hard_blocked_product_rejects_seller_edits(market):
    product, _ = market.listing()
    decision(market, product, hard=True)
    market.call("PATCH", B2B + f"/products/{product['id']}", market.seller, 403, json={"title": "Changed"})


def test_duplicate_event_same_idempotency_key_no_side_effects(market):
    product, _ = market.listing()
    event = decision(market, product)
    before = market.records("events")
    market.call("POST", B2B + "/moderation/events", SERVICE, 204, json=event)
    assert market.records("events") == before


def test_missing_service_key_returns_401(market):
    product, _ = market.listing()
    event = decision(market, product)
    market.call("POST", B2B + "/moderation/events", status=401, json=event)
