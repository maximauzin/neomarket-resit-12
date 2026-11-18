from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from .api import fail, principal, service
from .b2b import DB, apply_decision
from .storage import identifier, timestamp

router = APIRouter(prefix="/api/v1")


class ProductSnapshot(BaseModel):
    product_id: str
    seller_id: str
    category_id: str | None = None
    queue_priority: int = Field(default=3, ge=1, le=4)
    json_after: dict
    json_before: dict | None = None


class DeleteSnapshot(BaseModel):
    product_id: str


class ProductEvent(BaseModel):
    event_type: Literal["PRODUCT_CREATED", "PRODUCT_EDITED", "PRODUCT_DELETED"]
    idempotency_key: str = Field(min_length=1)
    occurred_at: datetime
    payload: dict


class Claim(BaseModel):
    queue_priority: int | None = Field(default=None, ge=1, le=4)
    category_ids: list[str] = Field(default_factory=list)


class Approval(BaseModel):
    comment: str | None = None


class Report(BaseModel):
    field_path: str
    message: str
    severity: Literal["INFO", "WARNING", "ERROR"] = "ERROR"


class Block(Approval):
    blocking_reason_ids: list[str] = Field(min_length=1)
    field_reports: list[Report] = Field(default_factory=list)


def receive_product(db, event):
    key, payload, event_type = event["idempotency_key"], event["payload"], event["event_type"]
    if db.get("moderation_inbox", key):
        return
    db.save("moderation_inbox", key, event)
    product_id = payload["product_id"]
    index = db.get("ticket_index", product_id)
    ticket = db.get("tickets", index["id"]) if index else None
    if event_type == "PRODUCT_DELETED":
        if ticket:
            db.remove("tickets", ticket["id"])
            db.remove("ticket_index", product_id)
        return
    if ticket and ticket["status"] == "HARD_BLOCKED":
        return
    if ticket and event_type == "PRODUCT_CREATED":
        return
    current_time = timestamp()
    if not ticket:
        ticket = dict(id=identifier(), product_id=product_id, created_at=current_time, revision=0)
    active_claim = ticket.get("status") == "IN_REVIEW"
    ticket.update(
        seller_id=payload["seller_id"], category_id=payload.get("category_id"),
        kind="EDIT" if event_type == "PRODUCT_EDITED" else "CREATE",
        status="IN_REVIEW" if active_claim else "PENDING",
        queue_priority=payload.get("queue_priority", 3),
        assigned_moderator_id=ticket.get("assigned_moderator_id") if active_claim else None,
        claimed_at=ticket.get("claimed_at") if active_claim else None,
        claim_expires_at=ticket.get("claim_expires_at") if active_claim else None,
        decision_at=None, updated_at=current_time, revision=ticket["revision"] + 1,
        json_before=payload.get("json_before"), json_after=payload["json_after"],
    )
    db.save("tickets", ticket["id"], ticket)
    db.save("ticket_index", product_id, {"id": ticket["id"]})


def ticket_response(ticket, *, with_content=False):
    fields = ("id", "product_id", "seller_id", "category_id", "kind", "status", "queue_priority",
              "assigned_moderator_id", "claimed_at", "claim_expires_at", "decision_at",
              "created_at", "updated_at")
    if with_content:
        fields += ("json_before", "json_after")
    return {field: ticket[field] for field in fields}


@router.post("/b2b/events", status_code=202, dependencies=[Depends(service)])
def events(body: ProductEvent, db: DB):
    model = DeleteSnapshot if body.event_type == "PRODUCT_DELETED" else ProductSnapshot
    try:
        payload = model.model_validate(body.payload).model_dump()
    except ValueError:
        fail(422, "INVALID_PAYLOAD", "Invalid product event payload")
    event = body.model_dump(mode="json")
    receive_product(db, dict(event, payload=payload))
    return Response(status_code=202)


@router.post("/queue/claim")
def claim(request: Request, db: DB, body: Claim = Claim()):
    moderator = principal(request, "moderator")
    instant = datetime.now(timezone.utc)
    now = instant.isoformat()
    choices = []
    for ticket in db.all("tickets"):
        if ticket["status"] == "IN_REVIEW" and ticket["claim_expires_at"] > now:
            if ticket["assigned_moderator_id"] == moderator:
                return ticket_response(ticket, with_content=True)
            continue
        if ticket["status"] not in {"PENDING", "IN_REVIEW"}:
            continue
        if body.queue_priority is not None and ticket["queue_priority"] != body.queue_priority:
            continue
        if body.category_ids and ticket["category_id"] not in body.category_ids:
            continue
        choices.append(ticket)
    if not choices:
        return Response(status_code=204)
    ticket = min(choices, key=lambda item: (item["queue_priority"], item["created_at"], item["id"]))
    ticket.update(status="IN_REVIEW", assigned_moderator_id=moderator, claimed_at=now,
                  claim_expires_at=(instant + timedelta(minutes=15)).isoformat(),
                  claimed_revision=ticket["revision"], updated_at=now)
    db.save("tickets", ticket["id"], ticket)
    return ticket_response(ticket, with_content=True)


def review_ticket(db, ticket_id, moderator):
    ticket = db.get("tickets", ticket_id)
    if not ticket:
        fail(404, "NOT_FOUND", "Ticket not found")
    if ticket["status"] != "IN_REVIEW" or ticket["claim_expires_at"] <= timestamp():
        fail(409, "INVALID_STATUS", "Ticket is no longer in review")
    if ticket["assigned_moderator_id"] != moderator:
        fail(403, "FORBIDDEN", "Ticket belongs to another moderator")
    if ticket["revision"] != ticket["claimed_revision"]:
        fail(409, "PRODUCT_CHANGED", "Product has changed during review")
    return ticket


def decide(db, ticket, moderator, comment, reason=None, reports=()):
    hard = bool(reason and reason["hard_block"])
    now = timestamp()
    ticket.update(status="HARD_BLOCKED" if hard else "BLOCKED" if reason else "APPROVED",
                  decision_at=now, updated_at=now)
    decision = dict(
        idempotency_key=identifier(), product_id=ticket["product_id"],
        moderator_id=moderator, moderator_comment=comment, hard_block=hard,
        event_type="BLOCKED" if reason else "MODERATED", occurred_at=now,
        blocking_reason_id=reason["id"] if reason else None, field_reports=list(reports),
    )
    db.save("tickets", ticket["id"], ticket)
    db.save("events", decision["idempotency_key"], decision)
    apply_decision(db, decision)
    return ticket_response(ticket)


@router.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str, request: Request, db: DB):
    principal(request, "moderator")
    ticket = db.get("tickets", ticket_id)
    if not ticket:
        fail(404, "NOT_FOUND", "Ticket not found")
    return ticket_response(ticket, with_content=True)


@router.post("/tickets/{ticket_id}/release")
def release(ticket_id: str, request: Request, db: DB):
    moderator = principal(request, "moderator")
    ticket = db.get("tickets", ticket_id)
    if not ticket:
        fail(404, "NOT_FOUND", "Ticket not found")
    if ticket["assigned_moderator_id"] != moderator:
        fail(403, "FORBIDDEN", "Ticket belongs to another moderator")
    if ticket["status"] != "IN_REVIEW":
        fail(409, "INVALID_STATUS", "Ticket is no longer in review")
    ticket.update(status="PENDING", assigned_moderator_id=None, claimed_at=None,
                  claim_expires_at=None, updated_at=timestamp())
    db.save("tickets", ticket_id, ticket)
    return ticket_response(ticket)


@router.post("/tickets/{ticket_id}/approve")
def approve(ticket_id: str, request: Request, db: DB, body: Approval = Approval()):
    moderator = principal(request, "moderator")
    ticket = review_ticket(db, ticket_id, moderator)
    product = db.get("products", ticket["product_id"])
    if not product or not product["sku_ids"]:
        fail(409, "NO_SKUS", "Product has no SKU")
    return decide(db, ticket, moderator, body.comment)


@router.post("/tickets/{ticket_id}/block")
def block(ticket_id: str, body: Block, request: Request, db: DB):
    moderator = principal(request, "moderator")
    ticket = review_ticket(db, ticket_id, moderator)
    reasons = [db.get("reasons", key) for key in dict.fromkeys(body.blocking_reason_ids)]
    if any(not reason or not reason["is_active"] for reason in reasons):
        fail(400, "INVALID_REASON", "Select active blocking reasons")
    reason = max(reasons, key=lambda item: item["hard_block"])
    reports = [dict(field_name=report.field_path, comment=report.message) for report in body.field_reports]
    return decide(db, ticket, moderator, body.comment, reason, reports)


@router.get("/blocking-reasons")
def blocking_reasons(request: Request, db: DB, hard_block: bool | None = None):
    principal(request, "moderator")
    return [reason for reason in db.all("reasons") if reason["is_active"]
            and (hard_block is None or hard_block == reason["hard_block"])]
