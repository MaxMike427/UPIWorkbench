"""JWT 认证依赖：支持 Authorization: Bearer 与 HttpOnly Cookie。"""
from __future__ import annotations

from datetime import timedelta

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import get_settings
from .utils import crypto

_bearer = HTTPBearer(auto_error=False)


async def _load_admin(username: str) -> dict | None:
    import aiosqlite

    from .database import get_db_path

    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, username FROM admins WHERE username=?", (username,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


def create_tokens(username: str) -> tuple[str, str]:
    s = get_settings()
    access = crypto.create_token(
        username, s.jwt_secret, timedelta(minutes=s.access_expire_minutes), "access"
    )
    refresh = crypto.create_token(
        username, s.jwt_secret, timedelta(days=s.refresh_expire_days), "refresh"
    )
    return access, refresh


def _extract_token(request: Request, creds: HTTPAuthorizationCredentials | None) -> str | None:
    if creds and creds.credentials:
        return creds.credentials
    cookie = request.cookies.get("access_token")
    if cookie:
        return cookie
    return None


async def get_current_admin(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    token = _extract_token(request, creds)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    try:
        payload = crypto.decode_token(token, get_settings().jwt_secret)
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 无效或已过期")
    if payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 类型错误")
    admin = await _load_admin(payload.get("sub", ""))
    if not admin:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在")
    return admin


async def get_admin_id(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> int | None:
    try:
        admin = await get_current_admin(request, creds)
        return admin["id"]
    except HTTPException:
        return None
