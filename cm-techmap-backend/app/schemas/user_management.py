"""CM TECHMAP — User Management Schemas"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr, Field


class UserCreateRequest(BaseModel):
    """Request to create a new user."""
    email: str = Field(..., description="User email address")
    first_name: str = Field(..., min_length=2, max_length=100)
    last_name: str = Field(..., min_length=2, max_length=100)
    password: str = Field(..., min_length=8)
    role: str = Field(default="viewer", description="Platform role")
    tenant_id: str = Field(default="", description="Tenant slug")
    department: str = Field(default="", description="Department name")


class UserUpdateRequest(BaseModel):
    """Request to update a user."""
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    enabled: Optional[bool] = None
    department: Optional[str] = None
    tenant_id: Optional[str] = None


class UserRoleChangeRequest(BaseModel):
    """Request to change a user's role."""
    role: str = Field(..., description="New platform role")


class PasswordResetRequest(BaseModel):
    """Request to reset a user's password."""
    new_password: str = Field(..., min_length=8)
    temporary: bool = Field(default=False, description="Require change on next login")


class UserResponse(BaseModel):
    """User response model."""
    id: str
    username: str
    email: str
    first_name: str
    last_name: str
    full_name: str
    enabled: bool
    email_verified: bool
    role: str
    all_roles: list[str]
    tenant_id: str
    department: str
    created_at: Optional[int] = None
    totp_enabled: bool = False
    active_sessions: int = 0


class UserListResponse(BaseModel):
    """Paginated user list response."""
    items: list[UserResponse]
    total: int
    page: int
    page_size: int


class UserStatsResponse(BaseModel):
    """Aggregate user statistics."""
    total_users: int
    active_sessions: int
    enabled_users: int
    disabled_users: int
    role_breakdown: dict[str, int]


class RoleResponse(BaseModel):
    """Available role response."""
    id: str
    name: str
    description: str
    hierarchy: int
