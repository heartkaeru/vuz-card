import os
from datetime import datetime, timedelta, timezone

import jwt
from passlib.context import CryptContext


password_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
JWT_ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return password_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return password_context.verify(password, password_hash)


def _jwt_secret() -> str:
    secret = os.environ.get("JWT_SECRET_KEY", "").strip()
    if len(secret.encode("utf-8")) < 32:
        raise RuntimeError("JWT_SECRET_KEY must contain at least 32 bytes")
    return secret


def validate_jwt_config() -> None:
    _jwt_secret()
    try:
        minutes = int(os.environ.get("JWT_ACCESS_TOKEN_MINUTES", "30"))
    except ValueError as exc:
        raise RuntimeError("JWT_ACCESS_TOKEN_MINUTES must be an integer") from exc
    if minutes <= 0:
        raise RuntimeError("JWT_ACCESS_TOKEN_MINUTES must be positive")


def create_access_token(user_id: str) -> tuple[str, int]:
    validate_jwt_config()
    minutes = int(os.environ.get("JWT_ACCESS_TOKEN_MINUTES", "1440"))
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    token = jwt.encode(
        {"sub": user_id, "iat": datetime.now(timezone.utc), "exp": expires_at},
        _jwt_secret(),
        algorithm=JWT_ALGORITHM,
    )
    return token, minutes * 60


def decode_access_token(token: str) -> dict:
    claims = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise jwt.InvalidTokenError("Token subject is missing")
    return claims
