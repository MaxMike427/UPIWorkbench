"""代理库持久化管理接口。"""
from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import get_current_admin
from ..database import get_db, log_action
from ..models import ProxyUpdate, ToggleRequest
from ..services.proxy_service import get_proxies, save_proxies

router=APIRouter(prefix='/api/proxies',tags=['proxies'])


@router.get('')
async def list_proxies(admin:dict=Depends(get_current_admin),db=Depends(get_db)):
    return await get_proxies(db)


@router.put('')
async def update_proxies(body:ProxyUpdate,request:Request,admin:dict=Depends(get_current_admin),db=Depends(get_db)):
    try:result=await save_proxies(db,body.text,body.enabled)
    except ValueError as e:raise HTTPException(status_code=400,detail=str(e))
    await log_action(db,admin['id'],'proxy_update','proxies',None,f"count={result['total']}",request.client.host if request.client else '')
    return result


@router.put('/enabled')
async def enable_proxies(body:ToggleRequest,request:Request,admin:dict=Depends(get_current_admin),db=Depends(get_db)):
    if body.enabled:
        async with db.execute('SELECT COUNT(*) FROM proxies') as cur:count=(await cur.fetchone())[0]
        if not count:raise HTTPException(status_code=400,detail='代理库为空，请先保存代理')
    await db.execute("UPDATE configs SET value=? WHERE key='proxy.enabled'",('true' if body.enabled else 'false',))
    await db.commit()
    await log_action(db,admin['id'],'proxy_toggle','proxies',None,'enabled' if body.enabled else 'disabled',request.client.host if request.client else '')
    return await get_proxies(db)
