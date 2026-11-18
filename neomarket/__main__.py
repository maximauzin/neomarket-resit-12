import argparse
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import jwt

from .main import create_app
from .storage import timestamp


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    token = commands.add_parser("token", help="Issue a local development JWT")
    token.add_argument("role", choices=["seller", "buyer", "moderator"])
    token.add_argument("--subject", type=UUID)
    stock = commands.add_parser("stock", help="Set stock on an existing SKU")
    stock.add_argument("sku")
    stock.add_argument("quantity", type=int)
    args = parser.parse_args()
    if args.command == "token":
        secret = os.environ.get("NEOMARKET_JWT_SECRET", "")
        if len(secret.encode()) < 32:
            parser.error("Set NEOMARKET_JWT_SECRET to at least 32 bytes")
        claims = {"sub": str(args.subject or uuid4()), "role": args.role,
                  "exp": datetime.now(timezone.utc) + timedelta(hours=1)}
        print(jwt.encode(claims, secret, algorithm="HS256"))
        return
    app = create_app()
    with app.state.database.transaction() as db:
        sku = db.get("skus", args.sku)
        if not sku:
            parser.error("SKU not found")
        if args.quantity < sku["reserved_quantity"]:
            parser.error("Stock must cover existing reservations")
        sku.update(stock_quantity=args.quantity, active_quantity=args.quantity - sku["reserved_quantity"],
                   updated_at=timestamp())
        db.save("skus", args.sku, sku)


if __name__ == "__main__":
    main()
