from uuid import uuid4

from conftest import B2B, MOD, SERVICE, actor
from neomarket.main import HARD_REASON
from neomarket.storage import timestamp


def incoming(market, product, event_type, **fields):
    event = {
        "event_type": event_type, "idempotency_key": str(uuid4()), "occurred_at": timestamp(),
        "payload": {"product_id": product["id"]} | fields,
    }
    market.call("POST", MOD + "/b2b/events", SERVICE, 202, json=event)


def test_approve_transitions_to_moderated_and_emits_event(market):
    product = market.product()
    market.sku(product)
    approved = market.approve(market.claim())
    assert approved["status"] == "APPROVED"
    assert market.get_product(product)["status"] == "MODERATED"
    assert any(event["event_type"] == "MODERATED" and event["product_id"] == product["id"]
               for event in market.records("events"))


def test_approve_others_card_returns_403(market):
    market.sku(market.product())
    market.approve(market.claim(), 403, actor("moderator")[1])


def test_approve_after_edited_returns_409(market):
    product = market.product()
    market.sku(product)
    ticket = market.claim()
    market.call("PATCH", B2B + f"/products/{product['id']}", market.seller, json={"title": "Edited"})
    assert market.approve(ticket, 409)["code"] == "PRODUCT_CHANGED"


def test_approve_without_sku_returns_409(market):
    product = market.product()
    incoming(market, product, "PRODUCT_CREATED", seller_id=market.seller_id, json_after={})
    assert market.approve(market.claim(), 409)["code"] == "NO_SKUS"


def block_product(market):
    product = market.product()
    sku = market.sku(product)
    ticket = market.claim()
    result = market.call("POST", MOD + f"/tickets/{ticket['id']}/block", market.moderator,
                         json={"blocking_reason_ids": [HARD_REASON], "comment": "Counterfeit"})
    return product, sku, ticket, result


def test_hard_block_transitions_to_terminal_and_emits_event(market):
    product, _, ticket, result = block_product(market)
    assert result["status"] == market.get_product(product)["status"] == "HARD_BLOCKED"
    market.approve(ticket, 409)
    assert any(event["event_type"] == "BLOCKED" for event in market.records("events"))


def test_hard_block_event_carries_hard_block_true(market):
    product, _, _, _ = block_product(market)
    [event] = [event for event in market.records("events") if event["event_type"] == "BLOCKED"]
    assert event["hard_block"] is True
    assert event["product_id"] == product["id"] and event["blocking_reason_id"] == HARD_REASON


def test_any_modify_on_hard_blocked_returns_403(market):
    product, sku, _, _ = block_product(market)
    for path in (f"/products/{product['id']}", f"/skus/{sku['id']}"):
        market.call("PATCH", B2B + path, market.seller, 403, json={})
        market.call("DELETE", B2B + path, market.seller, 403)


def test_edited_event_on_hard_blocked_is_ignored(market):
    product, _, _, _ = block_product(market)
    before = market.records("tickets")
    incoming(market, product, "PRODUCT_EDITED", seller_id=market.seller_id, json_after={"title": "Edit"})
    assert market.records("tickets") == before


def test_deleted_event_removes_hard_blocked(market):
    product, _, ticket, _ = block_product(market)
    incoming(market, product, "PRODUCT_DELETED")
    market.call("GET", MOD + f"/tickets/{ticket['id']}", market.moderator, 404)
    market.call("POST", MOD + "/queue/claim", market.moderator, 204)
