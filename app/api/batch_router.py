"""M6 批次编排：创建 / 启动全流程 / 暂停 / 继续 / 重试失败 / 进度 / SSE。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from ..auth import get_current_admin
from ..database import get_db, get_db_path, log_action
from ..models import BatchCreate, BatchOut, PageOut
from ..services.batch_service import BatchService
from ..services.success_service import export_successes
from ..services.report_service import write_task_report

router = APIRouter(prefix="/api/batches", tags=["batches"])


@router.post("", response_model=BatchOut)
async def create_batch(body: BatchCreate, request: Request,
                       admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    svc = BatchService(get_db_path())
    try:
        bid = await svc.create_batch(body.name, body.credential_ids,
                                     amount=body.amount, note=body.note, auto_push=body.auto_push)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await log_action(db, admin["id"], "create_batch", "batch", bid, body.name,
                     request.client.host if request.client else "")
    prog = await svc.get_progress(bid)
    return _out(prog)


def _out(prog: dict) -> dict:
    return {"id": prog["id"], "name": prog["name"],
            "total_credentials": prog["total_credentials"], "status": prog["status"],
            "current_step": prog.get("current_step"), "progress_json": prog.get("progress_json"),
            "amount": prog.get("amount") or 0, "note": prog.get("note") or "",
            "members": prog.get("members") or [], "source_batch_id": prog.get("source_batch_id"),
             "report_available":bool(prog.get("report_path")),
             "sms_enabled":prog.get('sms_enabled',True),'proxy_enabled':prog.get('proxy_enabled',False),
             'integration_enabled':prog.get('integration_enabled',False),
             'auto_push':prog.get('auto_push',True),
             'extract_provider':prog.get('extract_provider','ytc'),
             'payment_provider':prog.get('payment_provider','ytc'),
             'sms_provider':prog.get('sms_provider','handy'),
             'ytc_proxy_source':prog.get('ytc_proxy_source','auto')}


@router.get("", response_model=PageOut[BatchOut])
async def list_batches(page: int = Query(default=1, ge=1),
                       page_size: int = Query(default=20, ge=1, le=200),
                       admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    async with db.execute("SELECT COUNT(*) AS c FROM task_batches WHERE deleted_at IS NULL") as cur:
        total = (await cur.fetchone())["c"]
    async with db.execute(
        "SELECT id, name, total_credentials, status, current_step, progress_json,"
        " COALESCE(amount,0) AS amount, COALESCE(note,'') AS note, source_batch_id, report_path, snapshot_json"
        " FROM task_batches WHERE deleted_at IS NULL ORDER BY id DESC LIMIT ? OFFSET ?",
        (page_size, (page - 1) * page_size),
    ) as cur:
        rows = [dict(r) async for r in cur]
    for r in rows:
        r["members"] = []
        r["report_available"] = bool(r.pop("report_path",None))
        snapshot=json.loads(r.pop('snapshot_json') or '{}')
        r.update({key:snapshot.get(key,key=='sms_enabled') for key in ('sms_enabled','proxy_enabled','integration_enabled','auto_push','extract_provider','payment_provider','sms_provider','ytc_proxy_source')})
    return {"items": rows, "total": total, "page": page, "page_size": page_size}


@router.get("/{bid}/progress")
async def get_progress(bid: int, admin: dict = Depends(get_current_admin)):
    prog = await BatchService(get_db_path()).get_progress(bid)
    if not prog:
        raise HTTPException(status_code=404, detail="批次不存在")
    return _out(prog)


@router.get("/{bid}")
async def get_batch_detail(bid: int, admin: dict = Depends(get_current_admin)):
    prog = await BatchService(get_db_path()).get_progress(bid)
    if not prog:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _out(prog)


@router.get("/{bid}/events")
async def progress_events(bid: int, admin: dict = Depends(get_current_admin)):
    async def gen():
        svc = BatchService(get_db_path())
        for _ in range(900):  # 最长 ~30min
            prog = await svc.get_progress(bid)
            if not prog:
                yield 'event: error\ndata: {"detail":"批次不存在"}\n\n'
                return
            yield f"data: {json.dumps({'status': prog['status'], 'current_step': prog.get('current_step'), 'progress': json.loads(prog.get('progress_json') or '{}')}, ensure_ascii=False)}\n\n"
            if prog["status"] in ("completed", "failed"):
                return
            await asyncio.sleep(2)
        yield 'event: error\ndata: {"detail":"SSE 超时，请重连"}\n\n'

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/{bid}/export-success", response_class=PlainTextResponse)
async def export_batch_success(bid: int, request: Request,
                               admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    async with db.execute("SELECT id FROM task_batches WHERE id=?", (bid,)) as cur:
        if not await cur.fetchone():
            raise HTTPException(status_code=404, detail="任务不存在")
    try:
        content, count = await export_successes(db, batch_id=bid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await log_action(db, admin["id"], "success_export", "batch", bid, f"count={count}",
                     request.client.host if request.client else "")
    return PlainTextResponse(content, headers={
        "Content-Disposition": f"attachment; filename=task-{bid}-success.txt", "Cache-Control": "no-store",
    })


async def _svc_or_400(db,bid=None) -> BatchService:
    svc = await BatchService.from_db(db, get_db_path())
    if bid is not None:
        try:await svc.validate_start(db,bid)
        except ValueError as e:raise HTTPException(status_code=400,detail=str(e))
    return svc


@router.post("/{bid}/start")
async def start_batch(bid: int, request: Request,
                      admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    svc = await _svc_or_400(db,bid)
    prog = await svc.get_progress(bid)
    if not prog:
        raise HTTPException(status_code=404, detail="批次不存在")
    if prog["status"] == "running":
        raise HTTPException(status_code=409, detail="批次已在运行中")
    if prog["status"] != "created":
        raise HTTPException(status_code=409, detail="已执行的任务不能再次启动，请使用继续或失败重试")
    db2 = await svc._connect()
    try:
        await svc._set_status(db2, bid, "running")
    finally:
        await db2.close()
    try:
        svc.start(bid)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    await log_action(db, admin["id"], "start_batch", "batch", bid, "",
                     request.client.host if request.client else "")
    return {"ok": True, "status": "running"}


@router.post("/{bid}/pause")
async def pause_batch(bid: int, request: Request,
                      admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    svc = BatchService(get_db_path())
    prog = await svc.get_progress(bid)
    if not prog:
        raise HTTPException(status_code=404, detail="批次不存在")
    await svc.pause(bid)
    await log_action(db, admin["id"], "pause_batch", "batch", bid, "",
                     request.client.host if request.client else "")
    return {"ok": True, "status": "paused"}


@router.post("/{bid}/resume")
async def resume_batch(bid: int, request: Request,
                       admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    svc = await _svc_or_400(db,bid)
    try:
        await svc.resume(bid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await log_action(db, admin["id"], "resume_batch", "batch", bid, "",
                     request.client.host if request.client else "")
    return {"ok": True, "status": "running"}


@router.post("/{bid}/retry-failed")
async def retry_failed(bid: int, request: Request,
                       admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    svc = await _svc_or_400(db)
    try:
        retry = await svc.create_retry_batch(bid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        await svc.validate_start(db,retry['batch_id'])
        svc.start(retry["batch_id"])
    except (RuntimeError,ValueError) as e:
        await db.execute("UPDATE task_batches SET status='created' WHERE id=?",(retry['batch_id'],));await db.commit()
        raise HTTPException(status_code=400, detail=f'重试任务 #{retry["batch_id"]} 已创建，启动失败：{e}')
    await log_action(db, admin["id"], "retry_batch", "batch", retry["batch_id"],
                     f"source_batch={bid}",
                     request.client.host if request.client else "")
    return {"ok": True, "status": "running", **retry}


@router.delete("/{bid}")
async def delete_batch(bid: int, request: Request,
                       admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    async with db.execute("SELECT status FROM task_batches WHERE id=? AND deleted_at IS NULL", (bid,)) as cur:
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="批次不存在")
    from ..services.batch_service import _RUNNING
    worker=_RUNNING.get((get_db_path(),bid))
    if row["status"] == "running" or (worker and not worker.done()):
        raise HTTPException(status_code=409, detail="执行中的任务不可删除，请暂停并等待当前步骤结束")
    # 隐藏任务，不删除成员证据和产物，成功库与 MD 记录仍可追溯。
    await db.execute("UPDATE task_batches SET deleted_at=CURRENT_TIMESTAMP WHERE id=?",(bid,))
    await db.commit()
    await log_action(db, admin["id"], "delete_batch", "batch", bid, "",
                     request.client.host if request.client else "")
    return {"ok": True}


@router.get("/{bid}/report",response_class=PlainTextResponse)
async def task_report(bid:int,admin:dict=Depends(get_current_admin),db=Depends(get_db)):
    async with db.execute('SELECT status,report_path FROM task_batches WHERE id=?',(bid,)) as cur:
        row=await cur.fetchone()
    if not row:raise HTTPException(status_code=404,detail="任务不存在")
    if row['status'] not in ('completed','failed'):
        raise HTTPException(status_code=400,detail="任务完成后才会生成记录")
    path=row['report_path']
    if not path or not Path(path).exists():path=await write_task_report(db,bid)
    return PlainTextResponse(Path(path).read_text(encoding='utf-8'),media_type='text/markdown',headers={
        'Content-Disposition':f'attachment; filename=task-{bid}.md','Cache-Control':'no-store'})
