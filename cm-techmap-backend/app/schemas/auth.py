"""CM TECHMAP — Auth Schemas"""

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    email: str = Field(..., description="User email or username")
    password: str = Field(..., min_length=6)


class RegisterRequest(BaseModel):
    email: str = Field(..., description="Email address")
    password: str = Field(..., min_length=8)
    name: str = Field(..., min_length=2, max_length=255)
    tenant_slug: str = Field(
        default="default",
        description="Municipality slug (defaults to 'default' for self-registration)",
    )


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: dict | None = None


class TokenRefreshRequest(BaseModel):
    refresh_token: str


class UserInfo(BaseModel):
    sub: str
    email: str
    name: str
    preferred_username: str
    roles: list[str]
    tenant_id: str
    email_verified: bool
