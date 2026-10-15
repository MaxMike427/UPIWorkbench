"""凭证管理：导入 / 分页查询 / 导出 / 删除 / 批量删除。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse

from ..auth import get_current_admin
from ..database import get_db, log_action
from ..models import BatchDeleteRequest, ImportResult, PageOut, CredentialOut
from ..services import credential_service

router = APIRouter(prefix="/api/credentials", tags=["credentials"])


@router.post("/import", response_model=ImportResult)
async def import_txt(request: Request, file: UploadFile = File(...),
                     admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    raw = await file.read()
    try:
        content = raw.decode("utf-8-sig")  # 去 BOM
    except UnicodeDecodeError:
        content = raw.decode("gbk", errors="ignore")
    return await credential_service.import_from_txt(
        db, content, file.filename or "upload.txt",
        admin_id=admin["id"], ip=request.client.host if request.client else "")


@router.get("", response_model=PageOut[CredentialOut])
async def list_creds(status: str | None = Query(default=None),
                     keyword: str | None = Query(default=None),
                     source_file: str | None = Query(default=None),
                     page: int = Query(default=1, ge=1),
                     page_size: int = Query(default=20, ge=1, le=200),
                     admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    items, total = await credential_service.list_credentials(db, status, keyword, page, page_size, source_file)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/sources")
async def list_sources(admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    items = await credential_service.list_sources(db)
    return {"items": items, "total": len(items)}


@router.get("/export", response_class=PlainTextResponse)
async def export_txt(status: str | None = Query(default=None),
                     admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    return await credential_service.export_credentials(db, status)


@router.delete("/{cid}")
async def delete_one(cid: int, request: Request, admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    try:
        ok = await credential_service.delete_credential(db, cid)
    except ValueError as e:
        raise HTTPException(status_code=409,detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="记录不存在")
    await log_action(db, admin["id"], "delete", "credentials", cid, "",
                     request.client.host if request.client else "")
    return {"ok": True}


@router.post("/batch-delete")
async def batch_delete(body: BatchDeleteRequest, request: Request,
                       admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    try:
        n = await credential_service.batch_delete(db, body.ids)
    except ValueError as e:
        raise HTTPException(status_code=409,detail=str(e))
    await log_action(db, admin["id"], "batch-delete", "credentials", None, f"ids={body.ids} n={n}",
                     request.client.host if request.client else "")
    return {"ok": True, "deleted": n}
