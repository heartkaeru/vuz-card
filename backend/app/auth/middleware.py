import re

import jwt
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.auth.security import decode_access_token


_UNIVERSITY_WRITE_PATH = re.compile(r"^/api/v1/universities(?:/[^/]+)?/?$")
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class UniversityWriteAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        requires_auth = (
            request.method in _WRITE_METHODS
            and _UNIVERSITY_WRITE_PATH.fullmatch(request.url.path) is not None
        )
        if not requires_auth:
            return await call_next(request)

        authorization = request.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return self._unauthorized()
        try:
            request.state.auth_claims = decode_access_token(token.strip())
        except (jwt.InvalidTokenError, RuntimeError, ValueError):
            return self._unauthorized()
        return await call_next(request)

    @staticmethod
    def _unauthorized() -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={"detail": "Требуется авторизация"},
            headers={"WWW-Authenticate": "Bearer"},
        )
