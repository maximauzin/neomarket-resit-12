import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException

from . import b2b, b2c, moderation
from .api import handle_error
from .storage import Database

DEFAULT_CATEGORY = "4ca1fea4-b66d-454d-9b47-60cd59d39310"
HARD_REASON = "e311e1b4-e548-43d2-ab85-e81195e66def"
SOFT_REASON = "76311715-0d31-414c-9430-b70f3ff991b9"
logger = logging.getLogger(__name__)


def configure_database(database):
    with database.transaction() as db:
        if not db.get("categories", DEFAULT_CATEGORY):
            db.save("categories", DEFAULT_CATEGORY, {
                "id": DEFAULT_CATEGORY, "name": "Общие товары",
                "parent_id": None, "level": 0, "path": [DEFAULT_CATEGORY],
            })
        for key, code, title, hard in (
            (HARD_REASON, "COUNTERFEIT", "Контрафактный товар", True),
            (SOFT_REASON, "INACCURATE_DESCRIPTION", "Неточное описание", False),
        ):
            if not db.get("reasons", key):
                db.save("reasons", key, {"id": key, "code": code, "title": title,
                                        "description": None, "hard_block": hard, "is_active": True})


def retry_pending(database, gateway):
    with database.transaction() as db:
        for order in db.all("orders"):
            if order["status"] == "CANCEL_PENDING":
                b2c.cancel_order(db, order, gateway, order["cancel_reason"])


async def retry_loop(database, gateway):
    while True:
        await asyncio.sleep(15)
        try:
            await asyncio.to_thread(retry_pending, database, gateway)
        except Exception:
            logger.exception("Could not retry pending cancellations")


def create_app(db_path=None, *, jwt_secret=None, service_key=None, gateway=None):
    jwt_secret = jwt_secret or os.environ.get("NEOMARKET_JWT_SECRET", "")
    service_key = service_key or os.environ.get("NEOMARKET_SERVICE_KEY", "")
    if len(jwt_secret.encode()) < 32 or len(service_key.encode()) < 32:
        raise RuntimeError("Set NEOMARKET_JWT_SECRET and NEOMARKET_SERVICE_KEY to separate secrets of at least 32 bytes")
    if jwt_secret == service_key:
        raise RuntimeError("JWT and service secrets must differ")
    database = Database(db_path or Path(os.environ.get("NEOMARKET_DB", "neomarket.sqlite3")))
    configure_database(database)
    gateway = gateway or b2c.LocalB2B()

    @asynccontextmanager
    async def lifespan(app):
        retry_task = asyncio.create_task(retry_loop(database, gateway))
        try:
            yield
        finally:
            retry_task.cancel()
            with suppress(asyncio.CancelledError):
                await retry_task

    app = FastAPI(title="NeoMarket", lifespan=lifespan)
    for slug, module in (("b2b", b2b), ("b2c", b2c), ("moderation", moderation)):
        service_app = FastAPI(title=f"NeoMarket {slug.upper()}")
        service_app.state.database = database
        service_app.state.jwt_secret = jwt_secret
        service_app.state.service_key = service_key
        service_app.state.b2b = gateway
        service_app.add_exception_handler(HTTPException, handle_error)
        service_app.include_router(module.router)
        app.mount(f"/{slug}", service_app)
    app.state.database = database
    return app
