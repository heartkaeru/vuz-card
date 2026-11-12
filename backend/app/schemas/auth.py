from pydantic import BaseModel, EmailStr, Field, field_validator


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=5, max_length=72)

    @field_validator("password")
    @classmethod
    def bcrypt_password_must_fit(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 72:
            raise ValueError("Пароль должен занимать не более 72 байт")
        return value


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserResponse(BaseModel):
    id: str
    email: EmailStr
    role: str


class RegistrationResponse(BaseModel):
    user: UserResponse
    access_token: str
    token_type: str = "bearer"
    expires_in: int
