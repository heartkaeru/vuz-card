from fastapi import APIRouter, HTTPException, status

from app.auth.security import (
    create_access_token,
    hash_password,
    validate_jwt_config,
    verify_password,
)
from app.auth.store import create_user, get_user_by_email, initialize_auth_store
from app.schemas.auth import (
    Credentials,
    RegistrationResponse,
    TokenResponse,
)


router = APIRouter(prefix="/auth", tags=["Авторизация"])


@router.post(
    "/register",
    response_model=RegistrationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Зарегистрировать пользователя",
)
def register(payload: Credentials):
    validate_jwt_config()
    initialize_auth_store()
    email = str(payload.email).strip().lower()
    user = create_user(email, hash_password(payload.password))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Пользователь с таким email уже зарегистрирован",
        )
    token, expires_in = create_access_token(user["id"])
    return {"user": user, "access_token": token, "expires_in": expires_in}


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Войти и получить JWT",
)
def login(payload: Credentials):
    validate_jwt_config()
    initialize_auth_store()
    user = get_user_by_email(str(payload.email).strip().lower())
    if user is None or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный email или пароль",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token, expires_in = create_access_token(user["id"])
    return {"access_token": token, "expires_in": expires_in}
