import secrets

from fastapi import HTTPException, Request


def require_backend_token(request: Request) -> None:
    expected = request.app.state.settings.backend_auth_token.get_secret_value()
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(
            401, "Backend authentication required", headers={"WWW-Authenticate": "Bearer"}
        )
