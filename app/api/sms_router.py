"""M4 接码绑定接口：POST /api/sms/bind + 任务查询/重试。串行/小并发防风控。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..auth import get_current_admin
from ..database import get_db, log_action
from ..models import SMSBindRequest, SMSBindResult
from ..services.sms_service import SMSService

router = APIRouter(prefix="/api/sms", tags=["sms"])


async def _cfg(db, key: str, default: str = "") -> str:
    async with db.execute("SELECT value FROM configs WHERE key=?", (key,)) as cur:
        row = await cur.fetchone()
    return row["value"] if row else default


def _public(task: dict) -> dict:
    t = dict(task)
    for k in ("new_access_token",):
        v = t.get(k) or ""
        t[k] = (v[:12] + "…") if len(v) > 12 else v  # 新 AT 脱敏
    return t


@router.post("/bind", response_model=SMSBindResult)
async def bind(body: SMSBindRequest, request: Request,
               admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    ids = list(body.credential_ids)
    if not ids and body.status:
        async with db.execute("SELECT id FROM credentials WHERE status=? ORDER BY id",
                              (body.status,)) as cur:
            ids = [r["id"] async for r in cur]
    if not ids:
        raise HTTPException(status_code=400, detail="请提供 credential_ids 或有效的 status 筛选")
    base_url = await _cfg(db, "sms_provider.base_url", "https://apihub.space/handy")
    cdk = await _cfg(db, "sms_provider.cdk", "")
    if not cdk:
        raise HTTPException(status_code=400, detail="sms_provider.cdk 未配置")
    svc = SMSService(db=db, base_url=base_url, cdk=cdk, concurrency=body.concurrency,
                     max_retries=body.max_retries, retry_interval=body.retry_interval_s)
    res = await svc.bind_batch(ids, auto_retry=True)
    await log_action(db, admin["id"], "sms_bind", "credentials", None,
                     f"total={res.total} bound={res.bound} failed={res.failed}",
                     request.client.host if request.client else "")
    return {"total": res.total, "bound": res.bound, "failed": res.failed, "details": res.details}


@router.get("/tasks")
async def list_tasks(status: str | None = Query(default=None),
                     page: int = Query(default=1, ge=1),
                     page_size: int = Query(default=20, ge=1, le=200),
                     admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    args: list = []
    where = ""
    if status:
        where = "WHERE status=?"
        args.append(status)
    async with db.execute(f"SELECT COUNT(*) AS c FROM sms_tasks {where}", args) as cur:
        total = (await cur.fetchone())["c"]
    async with db.execute(
        f"SELECT * FROM sms_tasks {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        (*args, page_size, (page - 1) * page_size)) as cur:
        items = [_public(dict(r)) async for r in cur]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/tasks/{task_id}")
async def get_task(task_id: int, admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    async with db.execute("SELECT * FROM sms_tasks WHERE id=?", (task_id,)) as cur:
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _public(dict(row))


@router.post("/tasks/{task_id}/retry")
async def retry(task_id: int, request: Request,
                admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    base_url = await _cfg(db, "sms_provider.base_url", "https://apihub.space/handy")
    cdk = await _cfg(db, "sms_provider.cdk", "")
    svc = SMSService(db=db, base_url=base_url, cdk=cdk)
    try:
        t = await svc.retry_failed(task_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await log_action(db, admin["id"], "sms_retry", "sms_task", task_id, "",
                     request.client.host if request.client else "")
    return _public(t)
