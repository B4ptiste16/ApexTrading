"""
APEX Auth Server — Pydantic request / response schemas
"""

from typing import Optional
from pydantic import BaseModel


class SignupRequest(BaseModel):
    email:        str
    password:     str
    username:     Optional[str] = None
    display_name: Optional[str] = None


class LoginRequest(BaseModel):
    identifier: str   # email OR username
    password:   str


class UserInfo(BaseModel):
    id:           int
    username:     str
    email:        str
    display_name: str
    created_at:   str


class AuthResponse(BaseModel):
    token: str
    user:  dict
