"""成功库：展示全流程成功的账号，导出选中账号的三段 TXT。"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response

from ..auth import get_current_admin
from ..database import get_db, log_action
from ..services import success_service
from ..services.account_export_service import package_exports

router = APIRouter(prefix="/api/successes", tags=["successes"])


@router.get("")
async def list_successes(keyword: str | None = Query(default=None),
                        page: int = Query(default=1, ge=1),
                        page_size: int = Query(default=50, ge=1, le=200),
                        admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    return await success_service.list_successes(db, keyword, page, page_size)


@router.get("/export", response_class=PlainTextResponse)
async def export_successes(request: Request, ids: str | None = Query(default=None),
                           admin: dict = Depends(get_current_admin), db=Depends(get_db)):
    try:
        selected = success_service.parse_selected_ids(ids)
        content, count = await success_service.export_successes(db, ids=selected)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await log_action(db, admin["id"], "success_export", "credentials", None,
                     f"count={count}", request.client.host if request.client else "")
    return PlainTextResponse(content, headers={
        "Content-Disposition": "attachment; filename=success-credentials.txt", "Cache-Control": "no-store",
    })


@router.get('/download')
async def download_exports(request:Request,ids:str|None=Query(default=None),
                           format:Literal['cpa','sub2','both']=Query(default='both'),
                           admin:dict=Depends(get_current_admin),db=Depends(get_db)):
    try:
        selected=success_service.parse_selected_ids(ids)
        blob=await package_exports(db,selected,format)
    except ValueError as e:
        raise HTTPException(status_code=400,detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=409,detail=str(e))
    await log_action(db,admin['id'],'json_export','credentials',None,f'count={len(selected)} format={format}',
                     request.client.host if request.client else '')
    return Response(blob,media_type='application/zip',headers={
        'Content-Disposition':f'attachment; filename=success-{format}.zip','Cache-Control':'no-store'})
