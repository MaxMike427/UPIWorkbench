"""M5 提链接口：POST /api/upi/create + 任务查询/重试。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..auth import get_current_admin
from ..database import get_db, log_action
from ..models import UPICreateRequest, UPICreateResult
from ..services.upi_service import UPIService
from ..services.report_service import safe_error

router = APIRouter(prefix="/api/upi", tags=["upi"])


async def _cfg(db, key: str, default: str = "") -> str:
    async with db.execute("SELECT value FROM configs WHERE key=?", (key,)) as cur:
        row = await cur.fetchone()
    return row["value"] if row else default


def _public(task: dict) -> dict:
    t = dict(task)
    # 原始平台响应仅留在本地 DB，可能包含账号 token 或代理认证 URI。
    t.pop('raw_response',None)
    if t.get('error_msg'):t['error_msg']=safe_error(t['error_msg'])
    return t


async def _ytc_proxies(db,provider):
    if provider!='ytc':return None
    async with db.execute('SELECT url FROM proxies ORDER BY id') as cur:proxies=[r['url'] async for r in cur]
    if await _cfg(db,'proxy.enabled')!='true' or not proxies:
        raise HTTPException(status_code=400,detail='新提链需要开启代理库并保存有效代理')
    if any(proxy.split('://',1)[0].lower() not in ('http','https','socks5') for proxy in proxies):
        raise HTTPException(status_code=400,detail='任务代理包含 ytc 不支持的协议，仅支持 HTTP/HTTPS/SOCKS5')
    return proxies


@router.post("/create", response_model=UPICreateResult)
async def create(body: UPICreateRequest, request: Request,
                 admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    ids = list(body.credential_ids)
    if not ids and body.status:
        async with db.execute("SELECT id FROM credentials WHERE status=? ORDER BY id",
                              (body.status,)) as cur:
            ids = [r["id"] async for r in cur]
    if not ids:
        raise HTTPException(status_code=400, detail="请提供 credential_ids 或有效的 status 筛选")
    provider = await _cfg(db, "upi_provider.provider", "ytc")
    proxies=await _ytc_proxies(db,provider)
    ytc_base = await _cfg(db, "upi_provider.base_url", "https://ytc.xxsyun.xyz")
    ytc_key = await _cfg(db, "upi_provider.cdk", "") or await _cfg(db, "upi_provider.api_key", "")
    nerver_base = await _cfg(db, "qualification.api_url", "https://cha.nerver.cc")
    nerver_key = await _cfg(db, "qualification.api_key", "")
    promo_id = await _cfg(db, "qualification.promo_id", "plus-1-month-free")
    try:
        svc = UPIService(db=db, provider=provider, ytc_base_url=ytc_base, ytc_api_key=ytc_key,
                         nerver_base_url=nerver_base, nerver_api_key=nerver_key,
                         promo_id=promo_id, concurrency=body.concurrency,
                         max_retries=body.max_retries, retry_interval=body.retry_interval_s,ytc_proxies=proxies)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    res = await svc.create_batch(ids, amount=body.amount, note=body.note,
                                 callback_url=body.callback_url)
    await log_action(db, admin["id"], "upi_create", "credentials", None,
                     f"total={res.total} success={res.success} failed={res.failed}",
                     request.client.host if request.client else "")
    return {"total": res.total, "success": res.success, "failed": res.failed,
            "details": res.details}


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
    async with db.execute(f"SELECT COUNT(*) AS c FROM upi_tasks {where}", args) as cur:
        total = (await cur.fetchone())["c"]
    async with db.execute(
        f"SELECT * FROM upi_tasks {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        (*args, page_size, (page - 1) * page_size)) as cur:
        items = [_public(dict(r)) async for r in cur]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/tasks/{task_id}")
async def get_task(task_id: int, admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    async with db.execute("SELECT * FROM upi_tasks WHERE id=?", (task_id,)) as cur:
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _public(dict(row))


@router.post("/tasks/{task_id}/retry")
async def retry(task_id: int, request: Request,
                admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    provider = await _cfg(db, "upi_provider.provider", "ytc")
    proxies=await _ytc_proxies(db,provider)
    ytc_base = await _cfg(db, "upi_provider.base_url", "https://ytc.xxsyun.xyz")
    ytc_key = await _cfg(db, "upi_provider.cdk", "") or await _cfg(db, "upi_provider.api_key", "")
    nerver_base = await _cfg(db, "qualification.api_url", "https://cha.nerver.cc")
    nerver_key = await _cfg(db, "qualification.api_key", "")
    promo_id = await _cfg(db, "qualification.promo_id", "plus-1-month-free")
    try:
        svc = UPIService(db=db, provider=provider, ytc_base_url=ytc_base, ytc_api_key=ytc_key,
                         nerver_base_url=nerver_base, nerver_api_key=nerver_key,
                         promo_id=promo_id,ytc_proxies=proxies)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        t = await svc.retry_task(task_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await log_action(db, admin["id"], "upi_retry", "upi_task", task_id, "",
                     request.client.host if request.client else "")
    return _public(t)
