from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from neomarket.main import DEFAULT_CATEGORY, create_app

B2B = "/b2b/api/v1"
B2C = "/b2c/api/v1"
MOD = "/moderation/api/v1"
SECRET = "tests-signing-secret-long-enough-123"
SERVICE = {"X-Service-Key": "test-service-secret-very-long-12345"}


def actor(role):
    key = str(uuid4())
    token = jwt.encode({"sub": key, "role": role,
                        "exp": datetime.now(timezone.utc) + timedelta(hours=1)}, SECRET, algorithm="HS256")
    return key, {"Authorization": "Bearer " + token}


class Market:
    def __init__(self, app, client):
        self.app, self.client = app, client
        self.seller_id, self.seller = actor("seller")
        self.buyer_id, self.buyer = actor("buyer")
        self.moderator_id, self.moderator = actor("moderator")

    def call(self, method, path, headers=None, status=200, **kwargs):
        response = self.client.request(method, path, headers=headers, **kwargs)
        assert response.status_code == status, response.text
        return response.json() if response.content else None

    def product(self, **changes):
        data = {"title": "Test listing", "description": "Details", "category_id": DEFAULT_CATEGORY,
                "images": [{"url": "https://example.com/product.png"}]} | changes
        return self.call("POST", B2B + "/products", self.seller, 201, json=data)

    def sku(self, product, stock=5, **changes):
        data = {"product_id": product["id"], "name": "Variant", "price": 900, "cost_price": 400,
                "images": [{"url": "https://example.com/sku.png"}]} | changes
        sku = self.call("POST", B2B + "/skus", self.seller, 201, json=data)
        self.stock(sku, stock)
        return sku

    def stock(self, sku, stock):
        with self.app.state.database.transaction() as db:
            stored = db.get("skus", sku["id"])
            db.save("skus", sku["id"], dict(stored, stock_quantity=stock,
                active_quantity=stock - stored["reserved_quantity"]))

    def claim(self):
        return self.call("POST", MOD + "/queue/claim", self.moderator)

    def approve(self, ticket, status=200, headers=None):
        return self.call("POST", MOD + f"/tickets/{ticket['id']}/approve", headers or self.moderator, status)

    def listing(self, stock=5, **changes):
        product = self.product(**changes)
        sku = self.sku(product, stock)
        self.approve(self.claim())
        return product, sku

    def get_product(self, product, status=200, headers=None):
        return self.call("GET", B2B + f"/products/{product['id']}", headers or self.seller, status)

    def add_cart(self, sku, quantity=1, headers=None):
        return self.call("POST", B2C + "/cart/items", headers or self.buyer,
                         json={"sku_id": sku["id"], "quantity": quantity})

    def checkout_details(self):
        address = self.call("POST", B2C + "/buyers/me/addresses", self.buyer, 201,
            json={"country": "RU", "city": "Yekaterinburg", "street": "Main", "building": "1"})
        payment = self.call("POST", B2C + "/buyers/me/payment-methods", self.buyer, 201, json={"type": "WALLET"})
        return {"address_id": address["id"], "payment_method_id": payment["id"]}

    def checkout(self, details, key="d11f3020-7d33-46a9-a3b9-50b9c3e51ac2", status=201):
        return self.call("POST", B2C + "/orders", self.buyer | {"Idempotency-Key": key}, status, json=details)

    def records(self, collection):
        with self.app.state.database.transaction(write=False) as db:
            return db.all(collection)


@pytest.fixture
def market(tmp_path):
    app = create_app(tmp_path / "test.sqlite3", jwt_secret=SECRET, service_key=SERVICE["X-Service-Key"])
    with TestClient(app) as client:
        yield Market(app, client)
