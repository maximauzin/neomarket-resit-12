import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import b2b
from .api import attributes, fail, page, principal
from .b2b import DB, InventoryItem
from .storage import identifier, timestamp

router = APIRouter(prefix="/api/v1")


class B2BUnavailable(Exception):
    pass


class LocalB2B:
    catalog = staticmethod(b2b.service_catalog)
    reserve = staticmethod(b2b.reserve_stock)
    unreserve = staticmethod(b2b.release_stock)


class Quantity(BaseModel):
    quantity: int = Field(ge=0)


class Address(BaseModel):
    country: str = Field(min_length=1)
    region: str | None = None
    city: str = Field(min_length=1)
    street: str = Field(min_length=1)
    building: str = Field(min_length=1)
    apartment: str | None = None
    postal_code: str | None = None
    recipient_name: str | None = None
    recipient_phone: str | None = None
    is_default: bool = False
    comment: str | None = None


class Payment(BaseModel):
    type: Literal["CARD", "SBP", "WALLET"]
    card_last4: str | None = Field(default=None, pattern=r"^\d{4}$")
    card_brand: Literal["VISA", "MASTERCARD", "MIR"] | None = None
    is_default: bool = False


class OrderItemSnapshot(InventoryItem):
    unit_price: int = Field(ge=0)


class Checkout(BaseModel):
    address_id: str
    payment_method_id: str
    comment: str | None = None
    items_snapshot: list[OrderItemSnapshot] | None = None


class Cancellation(BaseModel):
    reason: str | None = None


def session_id(request):
    try:
        return str(UUID(request.headers.get("X-Session-Id", "")))
    except ValueError:
        fail(400, "SESSION_REQUIRED", "Guest carts require a valid X-Session-Id")


def cart_key(request):
    if request.headers.get("Authorization"):
        return "buyer:" + principal(request, "buyer")
    return "guest:" + session_id(request)


def item_view(db, entry):
    sku = db.get("skus", entry["sku_id"])
    if not sku:
        return dict(entry, available_quantity=0, is_available=False,
                    unavailable_reason="PRODUCT_DELETED")
    product = db.get("products", sku["product_id"])
    quantity = sku["active_quantity"]
    reason = None
    if not product or product["deleted"]:
        reason = "PRODUCT_DELETED"
    elif product["status"] != "MODERATED":
        reason = "PRODUCT_BLOCKED"
    elif quantity < entry["quantity"]:
        reason = "OUT_OF_STOCK" if not quantity else "QUANTITY_REDUCED"
    price = b2b.final_price(sku)
    return dict(entry, product_id=sku["product_id"], product_title=product["title"] if product else None,
                name=sku["name"], sku_code=sku["article"],
                unit_price=price, line_total=price * entry["quantity"],
                available_quantity=quantity if reason not in {"PRODUCT_DELETED", "PRODUCT_BLOCKED"} else 0,
                is_available=reason is None, unavailable_reason=reason,
                image=sku["images"][0] if sku["images"] else None)


def load_cart(db, key):
    return db.get("carts", key) or {"id": None, "items": {}, "updated_at": None}


def save_cart(db, key, cart):
    cart.update(id=cart["id"] or identifier(), updated_at=timestamp())
    db.save("carts", key, cart)


def cart_view(db, cart):
    items = [item_view(db, item) for item in cart["items"].values()]
    return dict(cart, items=items, items_count=sum(item["quantity"] for item in items),
                subtotal=sum(item["line_total"] for item in items if item["is_available"]),
                is_valid=all(item["is_available"] for item in items))


def validate_cart(db, cart, snapshot=None):
    view = cart_view(db, cart)
    issues = []
    expected = {item.sku_id: item.unit_price for item in snapshot} if snapshot is not None else {}
    for item in view["items"]:
        if not item["is_available"]:
            issues.append({"sku_id": item["sku_id"], "type": item["unavailable_reason"],
                           "message": "SKU is unavailable for the requested quantity",
                           "old_value": item["quantity"], "new_value": item["available_quantity"]})
        old_price = expected.get(item["sku_id"], item["unit_price_at_add"])
        if item["is_available"] and old_price != item["unit_price"]:
            issues.append({"sku_id": item["sku_id"], "type": "PRICE_CHANGED", "message": "Price changed",
                           "old_value": old_price, "new_value": item["unit_price"]})
    return {"is_valid": not issues, "cart": view, "issues": issues}


def ensure_session_stock(db, sku_id, quantity):
    sku = db.get("skus", sku_id)
    product = sku and db.get("products", sku["product_id"])
    if not product or product["deleted"]:
        fail(404, "NOT_FOUND", "SKU not found")
    if product["status"] != "MODERATED" or sku["active_quantity"] < quantity:
        fail(409, "SKU_UNAVAILABLE", "SKU is unavailable for the requested quantity")
    return sku


@router.get("/cart")
def get_cart(request: Request, db: DB):
    return cart_view(db, load_cart(db, cart_key(request)))


@router.delete("/cart", status_code=204)
def clear_cart(request: Request, db: DB):
    db.remove("carts", cart_key(request))


@router.post("/cart/items")
def add_item(body: InventoryItem, request: Request, db: DB):
    key = cart_key(request)
    cart = load_cart(db, key)
    current = cart["items"].get(body.sku_id)
    quantity = body.quantity + (current["quantity"] if current else 0)
    sku = ensure_session_stock(db, body.sku_id, quantity)
    price = b2b.final_price(sku)
    cart["items"][body.sku_id] = dict(
        (current or {}), sku_id=body.sku_id, quantity=quantity, unit_price_at_add=price,
        product_id=sku["product_id"], name=sku["name"], sku_code=sku["article"],
        unit_price=price, line_total=quantity * price,
    )
    save_cart(db, key, cart)
    return cart_view(db, cart)


@router.patch("/cart/items/{sku_id}")
def update_item(sku_id: str, body: Quantity, request: Request, db: DB):
    key = cart_key(request)
    cart = load_cart(db, key)
    if sku_id not in cart["items"]:
        fail(404, "NOT_FOUND", "Cart item not found")
    if body.quantity:
        sku = ensure_session_stock(db, sku_id, body.quantity)
        price = b2b.final_price(sku)
        cart["items"][sku_id].update(quantity=body.quantity, unit_price_at_add=price,
                                     unit_price=price, line_total=price * body.quantity)
    else:
        del cart["items"][sku_id]
    save_cart(db, key, cart)
    return cart_view(db, cart)


@router.delete("/cart/items/{sku_id}")
def remove_item(sku_id: str, request: Request, db: DB):
    key = cart_key(request)
    cart = load_cart(db, key)
    cart["items"].pop(sku_id, None)
    save_cart(db, key, cart)
    return cart_view(db, cart)


@router.post("/cart/validate")
def validate(request: Request, db: DB):
    return validate_cart(db, load_cart(db, cart_key(request)))


@router.post("/cart/merge")
def merge(request: Request, db: DB):
    key = "buyer:" + principal(request, "buyer")
    guest_key = "guest:" + session_id(request)
    cart, guest = load_cart(db, key), load_cart(db, guest_key)
    if guest["items"]:
        for sku_id, item in guest["items"].items():
            existing = cart["items"].get(sku_id)
            if existing:
                existing["quantity"] = max(existing["quantity"], item["quantity"])
            else:
                cart["items"][sku_id] = item
        save_cart(db, key, cart)
        db.remove("carts", guest_key)
    return cart_view(db, cart)


def buyer_sku(sku):
    return {
        "id": sku["id"], "name": sku["name"], "sku_code": sku["article"],
        "price": b2b.final_price(sku), "old_price": sku["price"] if sku["discount"] else None,
        "available_quantity": sku["active_quantity"], "is_available": sku["active_quantity"] > 0,
        "in_stock": sku["active_quantity"] > 0,
        "attributes": attributes(sku["characteristics"]), "images": sku["images"],
    }


def product_card(db, product):
    skus = [buyer_sku(sku) for sku in product["skus"]]
    available = [sku for sku in skus if sku["is_available"]]
    cheapest = min(available or skus, key=lambda sku: sku["price"])
    category = db.get("categories", product["category_id"])
    return dict(
        id=product["id"], name=product["title"], slug=product["slug"], category=category,
        min_price=cheapest["price"], old_price=cheapest["old_price"], has_stock=bool(available),
        images=product["images"], seller={"id": product["seller_id"]}, rating=None, reviews_count=0,
    )


def catalog_filters(request):
    params = request.query_params
    filters = {}
    for key in params:
        prefix = "filter[attributes]["
        if key.startswith(prefix) and key.endswith("]"):
            filters[key[len(prefix):-1]] = params.getlist(key)
    try:
        prices = {
            name: int(params[f"filter[{name}]"]) if f"filter[{name}]" in params else None
            for name in ("price_min", "price_max")
        }
    except ValueError:
        fail(400, "INVALID_FILTERS", "Prices must be integers")
    if any(value is not None and value < 0 for value in prices.values()):
        fail(400, "INVALID_FILTERS", "Prices must be non-negative")
    return dict(category_id=params.get("filter[category_id]"), seller_id=params.get("filter[seller_id]"),
                min_price=prices["price_min"], max_price=prices["price_max"],
                filters=b2b.filter_values(filters))


def catalog_query(request, db, search, sort):
    sorts = {"price_asc": "price_asc", "price_desc": "price_desc",
             "popularity": "popular", "new": "created_desc"}
    if sort not in sorts:
        fail(400, "INVALID_SORT", "Unsupported sort")
    try:
        return request.app.state.b2b.catalog(db, search=search, sort=sorts[sort],
                                             **catalog_filters(request))
    except B2BUnavailable:
        fail(502, "B2B_UNAVAILABLE", "Product catalog is temporarily unavailable")


@router.get("/catalog/products")
def catalog(request: Request, db: DB, q: Annotated[str | None, Query(max_length=200)] = None,
            sort: str = "popularity", limit: Annotated[int, Query(ge=1, le=100)] = 20,
            offset: Annotated[int, Query(ge=0)] = 0):
    return page([product_card(db, p) for p in catalog_query(request, db, q, sort)], limit, offset)


@router.get("/catalog/facets")
def facets(request: Request, db: DB, q: str | None = None):
    counts = {}
    for product in catalog_query(request, db, q, "new"):
        for name, value in attributes(product["characteristics"]).items():
            by_value = counts.setdefault(name, {})
            by_value[value] = by_value.get(value, 0) + 1
    return {"facets": [{"name": key, "values": [{"value": value, "count": count}
                         for value, count in sorted(values.items())]}
                        for key, values in sorted(counts.items())]}


@router.get("/catalog/products/{product_id}")
def product_detail(product_id: str, db: DB):
    data = b2b.public_product(product_id, db)
    return dict(product_card(db, data), description=data["description"],
                attributes=attributes(data["characteristics"]), skus=[buyer_sku(s) for s in data["skus"]])


def owned(db, collection, key, buyer):
    data = db.get(collection, key)
    if not data or data["buyer_id"] != buyer:
        fail(404, "NOT_FOUND", "Record not found")
    return data


def buyer_record(db, collection, buyer, body):
    key = identifier()
    value = db.save(collection, key, dict(body.model_dump(), id=key, buyer_id=buyer, created_at=timestamp()))
    return without_buyer(value)


def without_buyer(value):
    return {key: data for key, data in value.items() if key != "buyer_id"}


@router.post("/buyers/me/addresses", status_code=201)
def add_address(body: Address, request: Request, db: DB):
    return buyer_record(db, "addresses", principal(request, "buyer"), body)


@router.post("/buyers/me/payment-methods", status_code=201)
def add_payment(body: Payment, request: Request, db: DB):
    return buyer_record(db, "payments", principal(request, "buyer"), body)


def set_status(order, status, reason=None):
    order["status"] = status
    order["status_history"].append({"status": status, "changed_at": timestamp(), "reason": reason})


@router.post("/orders", status_code=201)
def checkout(body: Checkout, request: Request, db: DB):
    buyer = principal(request, "buyer")
    try:
        key = str(UUID(request.headers.get("Idempotency-Key", "")))
    except ValueError:
        fail(400, "IDEMPOTENCY_REQUIRED", "Provide a UUID as Idempotency-Key")
    scoped_key = f"{buyer}:{key}"
    existing = db.get("checkout_keys", scoped_key)
    instant = datetime.now(timezone.utc)
    payload = body.model_dump(mode="json")
    if existing and existing["expires_at"] > instant.isoformat():
        if existing["payload"] != payload:
            fail(409, "IDEMPOTENCY_CONFLICT", "Idempotency key was used with a different request")
        return owned(db, "orders", existing["id"], buyer)
    address = owned(db, "addresses", body.address_id, buyer)
    payment = owned(db, "payments", body.payment_method_id, buyer)
    cart = load_cart(db, "buyer:" + buyer)
    if not cart["items"]:
        fail(400, "EMPTY_CART", "Cart is empty")
    if body.items_snapshot is not None:
        snapshot = {item.sku_id: item.quantity for item in body.items_snapshot}
        expected = {item["sku_id"]: item["quantity"] for item in cart["items"].values()}
        if snapshot != expected or len(snapshot) != len(body.items_snapshot):
            fail(409, "CART_CHANGED", "Cart items changed")
    validation = validate_cart(db, cart, body.items_snapshot)
    if not validation["is_valid"]:
        return JSONResponse(validation, status_code=422)
    items = [
        {field: item[field] for field in ("sku_id", "product_id", "product_title", "name", "sku_code", "quantity",
                                         "unit_price", "line_total")}
        | {"image_url": item["image"]["url"] if item["image"] else None}
        for item in validation["cart"]["items"]
    ]
    order_id, now = identifier(), timestamp()
    try:
        stock_items = [InventoryItem.model_validate(item) for item in items]
        reserve_key = scoped_key if not existing else f"{scoped_key}:{order_id}"
        request.app.state.b2b.reserve(db, order_id, reserve_key, stock_items)
    except B2BUnavailable:
        fail(503, "B2B_UNAVAILABLE", "Stock service is temporarily unavailable")
    except HTTPException as error:
        if error.status_code != 409:
            raise
        fail(409, "RESERVE_FAILED", "Could not reserve all items",
             failed_items=error.detail["details"].get("failed_items", [error.detail["details"]]))
    subtotal = sum(item["line_total"] for item in items)
    order = dict(
        id=order_id, number=order_id[:8].upper(), buyer_id=buyer, status="CREATED",
        status_history=[{"status": "CREATED", "changed_at": now}], items=items,
        subtotal=subtotal, delivery_cost=0, total=subtotal, address=without_buyer(address),
        payment_method=without_buyer(payment), comment=body.comment, cancel_reason=None,
        created_at=now, paid_at=now, delivered_at=None,
    )
    set_status(order, "PAID")
    db.save("orders", order_id, order)
    db.save("checkout_keys", scoped_key, {
        "id": order_id, "payload": payload, "expires_at": (instant + timedelta(hours=1)).isoformat(),
    })
    db.remove("carts", "buyer:" + buyer)
    return order


@router.get("/orders")
def orders(request: Request, db: DB, status: str | None = None,
           limit: Annotated[int, Query(ge=1, le=100)] = 20,
           offset: Annotated[int, Query(ge=0)] = 0):
    buyer = principal(request, "buyer")
    items = [order for order in db.all("orders") if order["buyer_id"] == buyer
             and (status is None or order["status"] == status)]
    return page(sorted(items, key=lambda order: order["created_at"], reverse=True), limit, offset)


@router.get("/orders/{order_id}")
def order_details(order_id: str, request: Request, db: DB):
    return owned(db, "orders", order_id, principal(request, "buyer"))


def cancel_order(db, order, gateway, reason):
    stock_items = [InventoryItem.model_validate(item) for item in order["items"]]
    db.connection.execute("SAVEPOINT cancel_stock")
    try:
        gateway.unreserve(db, order["id"], stock_items)
    except (B2BUnavailable, sqlite3.OperationalError):
        db.connection.execute("ROLLBACK TO cancel_stock")
        if order["status"] != "CANCEL_PENDING":
            set_status(order, "CANCEL_PENDING", reason)
    else:
        set_status(order, "CANCELLED", reason)
    finally:
        db.connection.execute("RELEASE cancel_stock")
    order["cancel_reason"] = reason
    return db.save("orders", order["id"], order)


@router.post("/orders/{order_id}/cancel")
def cancel(order_id: str, request: Request, db: DB, body: Cancellation = Cancellation()):
    order = owned(db, "orders", order_id, principal(request, "buyer"))
    if order["status"] not in {"CREATED", "PAID", "ASSEMBLING", "DELIVERING"}:
        fail(409, "CANCEL_NOT_ALLOWED", "This order cannot be cancelled", current_status=order["status"])
    return cancel_order(db, order, request.app.state.b2b, order["cancel_reason"] or body.reason)
