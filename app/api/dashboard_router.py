"""仪表盘统计 + 最近批次 + 操作日志（M1 基础版）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..auth import get_current_admin
from ..config import get_settings
from ..database import get_db
from ..services.success_service import count_successes

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/stats")
async def stats(admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    async with db.execute("SELECT COUNT(*) AS c FROM credentials") as cur:
        total = (await cur.fetchone())["c"]
    async with db.execute("SELECT status, COUNT(*) AS c FROM credentials GROUP BY status") as cur:
        by_status = {r["status"]: r["c"] async for r in cur}
    async with db.execute("SELECT COUNT(*) AS c FROM task_batches WHERE deleted_at IS NULL") as cur:
        batches = (await cur.fetchone())["c"]
    async with db.execute("SELECT COUNT(*) AS c FROM sms_tasks") as cur:
        sms = (await cur.fetchone())["c"]
    async with db.execute("SELECT COUNT(*) AS c FROM upi_tasks") as cur:
        upi = (await cur.fetchone())["c"]
    success = await count_successes(db)
    return {"credentials_total": total, "by_status": by_status,
            "batches_total": batches, "sms_total": sms, "upi_total": upi,
            "success_total": success}


@router.get("/meta")
async def meta(admin: dict = Depends(get_current_admin)):
    return {"purchase_url": get_settings().purchase_url}


@router.get("/recent-batches")
async def recent_batches(limit: int = Query(default=5, ge=1, le=20),
                         admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    async with db.execute(
        "SELECT id, name, total_credentials, status, current_step FROM task_batches"
        " WHERE deleted_at IS NULL ORDER BY id DESC LIMIT ?", (limit,)) as cur:
        return [dict(r) async for r in cur]


@router.get("/logs")
async def logs(page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=200),
               admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    async with db.execute("SELECT COUNT(*) AS c FROM audit_logs") as cur:
        total = (await cur.fetchone())["c"]
    async with db.execute(
        "SELECT id, admin_id, action, target_type, target_id, detail, ip, created_at"
        " FROM audit_logs ORDER BY id DESC LIMIT ? OFFSET ?",
        (page_size, (page - 1) * page_size)) as cur:
        items = [dict(r) async for r in cur]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/runtime-logs")
async def runtime_logs(after_id: int | None = Query(default=None, ge=0), limit: int = Query(default=100, ge=1, le=500),
                       admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    if after_id is None:
        sql="SELECT id,level,message,batch_id,created_at FROM runtime_logs ORDER BY id DESC LIMIT ?"
        args=(limit,)
    else:
        sql="SELECT id,level,message,batch_id,created_at FROM runtime_logs WHERE id>? ORDER BY id ASC LIMIT ?"
        args=(after_id,limit)
    async with db.execute(sql,args) as cur:items=[dict(r) async for r in cur]
    items.sort(key=lambda item:item['id'])
    return {"items":items,"last_id":items[-1]['id'] if items else (after_id or 0)}
