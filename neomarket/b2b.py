import json
import re
from collections import Counter
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from .api import attributes, fail, page, principal, service, store
from .storage import identifier, timestamp

router = APIRouter(prefix="/api/v1")
service_only = [Depends(service)]
DB = Annotated[object, Depends(store, scope="function")]


class Image(BaseModel):
    url: str = Field(min_length=1)
    ordering: int = Field(default=0, ge=0)


class Characteristic(BaseModel):
    name: str
    value: str


class ProductCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1)
    category_id: str | None = None
    slug: str | None = None
    images: list[Image] = Field(default_factory=list)
    characteristics: list[Characteristic] = Field(default_factory=list)


class ProductUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, min_length=1)
    category_id: str | None = None
    characteristics: list[Characteristic] | None = None


class SKUCreate(BaseModel):
    product_id: str
    name: str = Field(min_length=1, max_length=255)
    price: int = Field(ge=0)
    discount: int = Field(default=0, ge=0)
    cost_price: int | None = Field(default=None, ge=0)
    article: str | None = None
    images: list[Image] = Field(default_factory=list)
    characteristics: list[Characteristic] = Field(default_factory=list)


class SKUUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    price: int | None = Field(default=None, ge=0)
    discount: int | None = Field(default=None, ge=0)
    cost_price: int | None = Field(default=None, ge=0)
    article: str | None = None
    characteristics: list[Characteristic] | None = None


class ProductIDs(BaseModel):
    product_ids: list[str] = Field(max_length=100)


class InventoryItem(BaseModel):
    sku_id: str
    quantity: int = Field(gt=0)


class InventoryOrder(BaseModel):
    order_id: str
    items: list[InventoryItem] = Field(min_length=1)


class Reserve(InventoryOrder):
    idempotency_key: str = Field(min_length=1)


class FieldReport(BaseModel):
    field_name: str
    sku_id: str | None = None
    comment: str


class Decision(BaseModel):
    idempotency_key: str = Field(min_length=1)
    product_id: str
    event_type: Literal["MODERATED", "BLOCKED"]
    moderator_id: str | None = None
    moderator_comment: str | None = None
    blocking_reason_id: str | None = None
    hard_block: bool = False
    field_reports: list[FieldReport] = Field(default_factory=list)
    occurred_at: str


def stamped(items):
    return [dict(item, id=identifier()) for item in items]


def product_skus(db, product):
    return [db.get("skus", key) for key in product["sku_ids"]]


def seller_product(db, seller, product_id, *, modify=False):
    product = db.get("products", product_id)
    if not product or product["seller_id"] != seller or product["deleted"]:
        fail(404, "NOT_FOUND", "Product not found")
    if modify and product["status"] == "HARD_BLOCKED":
        fail(403, "HARD_BLOCKED", "Product is permanently blocked")
    return product


def seller_view(db, product):
    data = {key: value for key, value in product.items() if key not in {"sku_ids", "revision"}}
    return dict(data, skus=product_skus(db, product))


def public_sku(sku):
    return {key: sku[key] for key in ("id", "product_id", "name", "price", "discount",
            "stock_quantity", "active_quantity", "article", "images", "characteristics")}


def public_view(db, product):
    data = {key: product[key] for key in ("id", "seller_id", "category_id", "title", "slug",
            "description", "status", "images", "characteristics", "created_at", "updated_at")}
    return dict(data, skus=[public_sku(sku) for sku in product_skus(db, product)])


def publish_product(db, event_type, product, previous=None):
    from .moderation import receive_product

    payload = {"product_id": product["id"]}
    if event_type != "PRODUCT_DELETED":
        payload.update(
            seller_id=product["seller_id"], category_id=product["category_id"],
            json_after=public_view(db, product),
        )
    if event_type == "PRODUCT_EDITED":
        payload["json_before"] = previous
    event = db.event(event_type, payload)
    receive_product(db, event)


def check_category(db, category_id):
    if not category_id or db.get("categories", category_id) is None:
        fail(400, "INVALID_CATEGORY", "An existing category is required")


@router.post("/products", status_code=201)
def create_product(body: ProductCreate, request: Request, db: DB):
    seller = principal(request, "seller")
    check_category(db, body.category_id)
    if not body.images:
        fail(400, "IMAGES_REQUIRED", "At least one image is required")
    key, now = identifier(), timestamp()
    data = body.model_dump()
    slug = body.slug or re.sub(r"[^\w]+", "-", body.title.lower()).strip("-") or "product"
    if body.slug and any(p["slug"] == body.slug for p in db.all("products")):
        fail(409, "SLUG_EXISTS", "Product slug is already in use")
    db.save("products", key, product := dict(
        data, id=key, seller_id=seller, created_at=now, updated_at=now,
        slug=slug if body.slug else f"{slug}-{key[:8]}",
        images=stamped(data["images"]), characteristics=stamped(data["characteristics"]),
        status="CREATED", deleted=False, blocked=False, blocking_reason=None,
        blocking_reason_id=None, moderator_comment=None, field_reports=[], sku_ids=[], revision=0,
    ))
    return seller_view(db, product)


@router.get("/products/{product_id}")
def get_product(product_id: str, request: Request, db: DB):
    if request.headers.get("X-Service-Key"):
        service(request)
        product = db.get("products", product_id)
        if not product or product["deleted"]:
            fail(404, "NOT_FOUND", "Product not found")
        return public_view(db, product)
    return seller_view(db, seller_product(db, principal(request, "seller"), product_id))


@router.patch("/products/{product_id}")
def update_product(product_id: str, body: ProductUpdate, request: Request, db: DB):
    product = seller_product(db, principal(request, "seller"), product_id, modify=True)
    previous = public_view(db, product)
    changes = body.model_dump(exclude_none=True)
    if "category_id" in changes:
        check_category(db, changes["category_id"])
    if "characteristics" in changes:
        changes["characteristics"] = stamped(changes["characteristics"])
    product.update(changes, updated_at=timestamp(), revision=product["revision"] + 1)
    if product["sku_ids"]:
        product["status"] = "ON_MODERATION"
    db.save("products", product_id, product)
    if product["sku_ids"]:
        publish_product(db, "PRODUCT_EDITED", product, previous)
    return seller_view(db, product)


@router.delete("/products/{product_id}", status_code=204)
def delete_product(product_id: str, request: Request, db: DB):
    product = seller_product(db, principal(request, "seller"), product_id, modify=True)
    product.update(deleted=True, updated_at=timestamp())
    db.save("products", product_id, product)
    publish_product(db, "PRODUCT_DELETED", product)


@router.post("/skus", status_code=201)
def create_sku(body: SKUCreate, request: Request, db: DB):
    product = seller_product(db, principal(request, "seller"), body.product_id, modify=True)
    if not body.images:
        fail(400, "IMAGES_REQUIRED", "At least one SKU image is required")
    if body.discount > body.price:
        fail(400, "INVALID_DISCOUNT", "Discount exceeds price")
    needs_review = product["status"] in {"MODERATED", "BLOCKED"}
    previous = public_view(db, product) if needs_review else None
    key, now = identifier(), timestamp()
    data = body.model_dump()
    sku = db.save("skus", key, dict(data, id=key, created_at=now, updated_at=now,
        article=body.article or key, images=stamped(data["images"]),
        characteristics=stamped(data["characteristics"]), stock_quantity=0,
        active_quantity=0, reserved_quantity=0))
    first = not product["sku_ids"] and product["status"] == "CREATED"
    product["sku_ids"].append(key)
    product["updated_at"] = now
    if first or needs_review:
        product["status"] = "ON_MODERATION"
    db.save("products", body.product_id, product)
    if first:
        publish_product(db, "PRODUCT_CREATED", product)
    elif needs_review:
        publish_product(db, "PRODUCT_EDITED", product, previous)
    return sku


@router.get("/products/{product_id}/skus")
def list_skus(product_id: str, request: Request, db: DB):
    return product_skus(db, seller_product(db, principal(request, "seller"), product_id))


def seller_sku(db, request, sku_id):
    seller = principal(request, "seller")
    sku = db.get("skus", sku_id)
    if not sku:
        fail(404, "NOT_FOUND", "SKU not found")
    return sku, seller_product(db, seller, sku["product_id"], modify=True)


@router.patch("/skus/{sku_id}")
def update_sku(sku_id: str, body: SKUUpdate, request: Request, db: DB):
    sku, product = seller_sku(db, request, sku_id)
    previous = public_view(db, product)
    changes = body.model_dump(exclude_none=True)
    if "characteristics" in changes:
        changes["characteristics"] = stamped(changes["characteristics"])
    sku.update(changes, updated_at=timestamp())
    if sku["discount"] > sku["price"]:
        fail(400, "INVALID_DISCOUNT", "Discount exceeds price")
    db.save("skus", sku_id, sku)
    if set(changes) - {"cost_price"}:
        product.update(status="ON_MODERATION", updated_at=timestamp())
        db.save("products", product["id"], product)
        publish_product(db, "PRODUCT_EDITED", product, previous)
    return sku


@router.delete("/skus/{sku_id}", status_code=204)
def delete_sku(sku_id: str, request: Request, db: DB):
    sku, product = seller_sku(db, request, sku_id)
    if sku["reserved_quantity"]:
        fail(409, "ACTIVE_RESERVATIONS", "SKU has active reservations")
    previous = public_view(db, product)
    db.remove("skus", sku_id)
    product["sku_ids"].remove(sku_id)
    product.update(status="ON_MODERATION" if product["sku_ids"] else "CREATED", updated_at=timestamp())
    db.save("products", product["id"], product)
    publish_product(db, "PRODUCT_EDITED", product, previous)


def visible(db, product):
    return product and not product["deleted"] and product["status"] == "MODERATED" and any(
        sku["active_quantity"] > 0 for sku in product_skus(db, product)
    )


def final_price(sku):
    return sku["price"] - sku["discount"]


def service_catalog(db, *, category_id=None, search=None, min_price=None, max_price=None,
                    seller_id=None, filters=None, sort="created_desc"):
    if sort not in {"price_asc", "price_desc", "created_desc", "popular"}:
        fail(400, "INVALID_SORT", "Unsupported sort")
    products = []
    for product in db.all("products"):
        if not visible(db, product):
            continue
        if category_id and product["category_id"] != category_id:
            continue
        if seller_id and product["seller_id"] != seller_id:
            continue
        if search and search.casefold() not in (product["title"] + " " + product["description"]).casefold():
            continue
        sku_prices = [
            final_price(sku) for sku in product_skus(db, product) if sku["active_quantity"] > 0
        ]
        lowest = min(sku_prices)
        if (min_price is not None and lowest < min_price) or (max_price is not None and lowest > max_price):
            continue
        values = attributes(product["characteristics"])
        if filters and any(values.get(key) not in choices for key, choices in filters.items()):
            continue
        products.append(dict(public_view(db, product), min_price=lowest))
    sort_key = {"price_asc": "min_price", "price_desc": "min_price",
                "created_desc": "created_at", "popular": "created_at"}[sort]
    return sorted(products, key=lambda product: (product[sort_key], product["id"]),
                  reverse=sort != "price_asc")


def filter_values(data):
    if not isinstance(data, dict):
        fail(400, "INVALID_FILTERS", "Expected a filter object")
    result = {}
    for key, values in data.items():
        values = values if isinstance(values, list) else [values]
        result[key] = {str(value) for value in values}
    return result


@router.get("/public/products", dependencies=service_only)
def public_products(db: DB, category_id: str | None = None, search: str | None = None,
                    min_price: Annotated[int | None, Query(ge=0)] = None,
                    max_price: Annotated[int | None, Query(ge=0)] = None,
                    seller_id: str | None = None, filters: str | None = None,
                    sort: str = "created_desc", limit: Annotated[int, Query(ge=1, le=100)] = 20,
                    offset: Annotated[int, Query(ge=0)] = 0):
    try:
        selected = filter_values(json.loads(filters)) if filters else None
    except ValueError:
        fail(400, "INVALID_FILTERS", "Invalid filter JSON")
    products = service_catalog(db, category_id=category_id, search=search,
        min_price=min_price, max_price=max_price, seller_id=seller_id, filters=selected, sort=sort)
    items = [{key: p[key] for key in ("id", "title", "slug", "status", "category_id",
              "min_price", "created_at")} | {"cover_image": p["images"][0]["url"]} for p in products]
    return page(items, limit, offset)


@router.post("/public/products/batch", dependencies=service_only)
def batch_products(body: ProductIDs, db: DB):
    products = [db.get("products", key) for key in dict.fromkeys(body.product_ids)]
    return [public_view(db, product) for product in products if visible(db, product)]


@router.get("/public/products/{product_id}", dependencies=service_only)
def public_product(product_id: str, db: DB):
    product = db.get("products", product_id)
    if not visible(db, product):
        fail(404, "NOT_FOUND", "Product not found")
    return public_view(db, product)


@router.get("/public/skus/{sku_id}", dependencies=service_only)
def get_public_sku(sku_id: str, db: DB):
    sku = db.get("skus", sku_id)
    product = sku and db.get("products", sku["product_id"])
    if not product or product["deleted"] or product["status"] != "MODERATED":
        fail(404, "NOT_FOUND", "SKU not found")
    return public_sku(sku)


def item_quantities(items):
    quantities = Counter()
    for item in items:
        quantities[item.sku_id] += item.quantity
    return dict(quantities)


def reserve_stock(db, order_id, key, items):
    quantities = item_quantities(items)
    previous = db.get("reservation_keys", key)
    reservation = db.get("reservations", order_id)
    if previous or reservation:
        if not reservation or (previous and previous["order_id"] != order_id) or reservation["quantities"] != quantities:
            fail(409, "RESERVATION_CONFLICT", "Reservation has different items or order")
        if reservation["status"] != "RESERVED":
            fail(409, "RESERVATION_CLOSED", "Reservation has already been released")
        db.save("reservation_keys", key, {"order_id": order_id})
        return reservation["response"]
    skus = {key: db.get("skus", key) for key in quantities}
    failed = []
    for sku_id, quantity in quantities.items():
        sku = skus[sku_id]
        product = sku and db.get("products", sku["product_id"])
        available = sku["active_quantity"] if product and not product["deleted"] and product["status"] == "MODERATED" else 0
        if available < quantity:
            failed.append({"sku_id": sku_id, "requested": quantity, "available": available})
    if failed:
        fail(409, "INSUFFICIENT_STOCK", "Unable to reserve SKU", failed_items=failed)
    now = timestamp()
    for sku_id, quantity in quantities.items():
        sku = skus[sku_id]
        sku["active_quantity"] -= quantity
        sku["reserved_quantity"] += quantity
        sku["updated_at"] = now
        db.save("skus", sku_id, sku)
        if sku["active_quantity"] == 0:
            db.event("SKU_OUT_OF_STOCK", {"sku_id": sku_id, "product_id": sku["product_id"]})
    response = {"order_id": order_id, "status": "RESERVED", "reserved_at": now}
    db.save("reservations", order_id, {"status": "RESERVED", "quantities": quantities, "response": response})
    db.save("reservation_keys", key, {"order_id": order_id})
    return response


def release_stock(db, order_id, items):
    reservation = db.get("reservations", order_id)
    quantities = item_quantities(items)
    if not reservation or reservation["quantities"] != quantities:
        fail(409, "RESERVATION_CONFLICT", "Reservation does not match")
    if reservation["status"] == "UNRESERVED":
        return reservation["response"]
    now = timestamp()
    for sku_id, quantity in quantities.items():
        sku = db.get("skus", sku_id)
        sku["active_quantity"] += quantity
        sku["reserved_quantity"] -= quantity
        sku["updated_at"] = now
        db.save("skus", sku_id, sku)
    response = {"order_id": order_id, "status": "UNRESERVED", "processed_at": now}
    db.save("reservations", order_id, dict(reservation, status="UNRESERVED", response=response))
    return response


@router.post("/inventory/reserve", dependencies=service_only)
def reserve(body: Reserve, db: DB):
    return reserve_stock(db, body.order_id, body.idempotency_key, body.items)


@router.post("/inventory/unreserve", dependencies=service_only)
def unreserve(body: InventoryOrder, db: DB):
    return release_stock(db, body.order_id, body.items)


def apply_decision(db, event):
    key = event["idempotency_key"]
    if db.get("b2b_inbox", key):
        return
    product = db.get("products", event["product_id"])
    if not product or product["deleted"]:
        fail(404, "NOT_FOUND", "Product not found")
    db.save("b2b_inbox", key, event)
    if product["status"] == "HARD_BLOCKED":
        return
    if event["event_type"] == "MODERATED":
        product.update(status="MODERATED", blocked=False, blocking_reason=None,
                       blocking_reason_id=None, field_reports=[], moderator_comment=None)
        event_type = "PRODUCT_AVAILABLE"
    else:
        reason_id = event.get("blocking_reason_id")
        if not reason_id:
            fail(400, "REASON_REQUIRED", "Blocking reason is required")
        reason = db.get("reasons", reason_id) or {"title": reason_id}
        product.update(status="HARD_BLOCKED" if event["hard_block"] else "BLOCKED",
            blocked=True, blocking_reason={"id": reason_id, "title": reason["title"],
            "comment": event.get("moderator_comment")}, blocking_reason_id=reason_id,
            field_reports=event.get("field_reports", []), moderator_comment=event.get("moderator_comment"))
        event_type = "PRODUCT_BLOCKED"
    product["updated_at"] = timestamp()
    db.save("products", product["id"], product)
    db.event(event_type, {"product_id": product["id"], "hard_block": product["status"] == "HARD_BLOCKED"})


@router.post("/moderation/events", status_code=204, dependencies=service_only)
def moderation_event(body: Decision, db: DB):
    apply_decision(db, body.model_dump())
    return Response(status_code=204)
