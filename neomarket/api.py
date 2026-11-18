import hmac
from uuid import UUID

import jwt
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


def fail(status_code, code, message, **details):
    raise HTTPException(status_code, {"code": code, "message": message, "details": details})


async def handle_error(request, error):
    return JSONResponse(error.detail, status_code=error.status_code, headers=error.headers)


def store(request: Request):
    with request.app.state.database.transaction(write=request.method not in {"GET", "HEAD"}) as db:
        yield db


def principal(request, role):
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        fail(401, "UNAUTHORIZED", "Bearer token required")
    try:
        claims = jwt.decode(
            token, request.app.state.jwt_secret, algorithms=["HS256"],
            options={"require": ["exp", "sub", "role"]},
        )
        subject = str(UUID(claims["sub"]))
    except (jwt.InvalidTokenError, ValueError, TypeError):
        fail(401, "UNAUTHORIZED", "Invalid bearer token")
    if claims["role"] != role:
        fail(403, "FORBIDDEN", "Insufficient permissions")
    return subject


def service(request: Request):
    supplied = request.headers.get("X-Service-Key", "").encode()
    expected = request.app.state.service_key.encode()
    if not supplied or not hmac.compare_digest(supplied, expected):
        fail(401, "UNAUTHORIZED", "Invalid service key")


def page(items, limit, offset):
    return {"items": items[offset:offset + limit], "total_count": len(items),
            "limit": limit, "offset": offset}


def attributes(characteristics):
    return {item["name"]: item["value"] for item in characteristics}
