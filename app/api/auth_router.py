"""POST /api/auth/login|refresh, GET /api/auth/me"""
from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from ..auth import create_tokens, get_current_admin
from ..config import get_settings
from ..database import get_db, log_action
from ..models import AdminOut, LoginRequest, PasswordChangeRequest, RefreshRequest, TokenResponse
from ..utils import crypto

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, response: Response, db=Depends(get_db)):
    async with db.execute("SELECT id, username, password_hash FROM admins WHERE username=?",
                          (body.username,)) as cur:
        row = await cur.fetchone()
    if not row or not crypto.verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    access, refresh = create_tokens(row["username"])
    s = get_settings()
    response.set_cookie("access_token", access, httponly=True, samesite="lax", max_age=s.access_expire_minutes * 60)
    response.set_cookie("refresh_token", refresh, httponly=True, samesite="lax",
                        max_age=s.refresh_expire_days * 86400)
    await log_action(db, row["id"], "login", "admin", row["id"], "login ok",
                     request.client.host if request.client else "")
    return TokenResponse(access_token=access, refresh_token=refresh,
                         expires_in=s.access_expire_minutes * 60)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, request: Request, response: Response):
    token = body.refresh_token or request.cookies.get("refresh_token", "")
    if not token:
        raise HTTPException(status_code=401, detail="缺少 refresh_token")
    try:
        payload = crypto.decode_token(token, get_settings().jwt_secret)
    except Exception:
        raise HTTPException(status_code=401, detail="refresh_token 无效或已过期")
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Token 类型错误")
    access, new_refresh = create_tokens(payload["sub"])
    s = get_settings()
    response.set_cookie("access_token", access, httponly=True, samesite="lax", max_age=s.access_expire_minutes * 60)
    response.set_cookie("refresh_token", new_refresh, httponly=True, samesite="lax",
                        max_age=s.refresh_expire_days * 86400)
    return TokenResponse(access_token=access, refresh_token=new_refresh,
                         expires_in=s.access_expire_minutes * 60)


@router.get("/me", response_model=AdminOut)
async def me(admin: dict = Depends(get_current_admin)):
    return AdminOut(**admin)


@router.post("/password")
async def change_password(body: PasswordChangeRequest, request: Request,
                          admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    if len(body.new_password.encode('utf-8'))>72:
        raise HTTPException(status_code=400,detail="新密码不能超过 72 个 UTF-8 字节")
    if body.new_password != body.confirm_password:
        raise HTTPException(status_code=400, detail="两次输入的新密码不一致")
    async with db.execute("SELECT password_hash FROM admins WHERE id=?", (admin["id"],)) as cur:
        row = await cur.fetchone()
    if not row or not crypto.verify_password(body.current_password, row["password_hash"]):
        raise HTTPException(status_code=400, detail="当前密码错误")
    await db.execute("UPDATE admins SET password_hash=? WHERE id=?",
                     (crypto.hash_password(body.new_password), admin["id"]))
    await db.commit()
    await log_action(db, admin["id"], "password_change", "admin", admin["id"], "",
                     request.client.host if request.client else "")
    return {"ok": True}


@router.post('/logout')
async def logout(response:Response):
    response.delete_cookie('access_token')
    response.delete_cookie('refresh_token')
    return {"ok":True}
