"""代理库持久化管理接口。"""
from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import get_current_admin
from ..database import get_db, log_action
from ..models import DynamicRequest, ProxyIdsRequest, ProxyImportRequest, ProxyUpdate, ToggleRequest
from ..services.proxy_service import append_proxies, delete_proxies, get_proxies, save_proxies, set_dynamic, test_proxies, test_proxy

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


@router.post('')
async def add_proxies(body:ProxyImportRequest,request:Request,admin:dict=Depends(get_current_admin),db=Depends(get_db)):
    try:result=await append_proxies(db,body.text,body.enabled,body.is_dynamic)
    except ValueError as e:raise HTTPException(status_code=400,detail=str(e))
    await log_action(db,admin['id'],'proxy_add','proxies',None,f"count={result['total']},dynamic={body.is_dynamic}",request.client.host if request.client else '')
    return result


@router.put('/{pid}/dynamic')
async def mark_dynamic(pid:int,body:DynamicRequest,request:Request,admin:dict=Depends(get_current_admin),db=Depends(get_db)):
    try:result=await set_dynamic(db,pid,body.is_dynamic)
    except ValueError as e:raise HTTPException(status_code=404,detail=str(e))
    await log_action(db,admin['id'],'proxy_dynamic','proxy',pid,f"dynamic={body.is_dynamic}",request.client.host if request.client else '')
    return result


@router.post('/batch-delete')
async def batch_delete(body:ProxyIdsRequest,request:Request,admin:dict=Depends(get_current_admin),db=Depends(get_db)):
    deleted=await delete_proxies(db,list(dict.fromkeys(body.ids)))
    await log_action(db,admin['id'],'proxy_delete','proxies',None,f"count={deleted}",request.client.host if request.client else '')
    return {'deleted':deleted,**(await get_proxies(db))}


@router.post('/batch-test')
async def batch_test(body:ProxyIdsRequest,admin:dict=Depends(get_current_admin),db=Depends(get_db)):
    return {'results':await test_proxies(db,list(dict.fromkeys(body.ids)))}


@router.delete('/{pid}')
async def delete_proxy(pid:int,request:Request,admin:dict=Depends(get_current_admin),db=Depends(get_db)):
    deleted=await delete_proxies(db,[pid])
    if not deleted:raise HTTPException(status_code=404,detail='代理不存在')
    await log_action(db,admin['id'],'proxy_delete','proxy',pid,'',request.client.host if request.client else '')
    return {'deleted':deleted,**(await get_proxies(db))}


@router.post('/{pid}/test')
async def check_proxy(pid:int,admin:dict=Depends(get_current_admin),db=Depends(get_db)):
    try:return await test_proxy(db,pid)
    except ValueError as e:raise HTTPException(status_code=404,detail=str(e))


@router.put('/enabled')
async def enable_proxies(body:ToggleRequest,request:Request,admin:dict=Depends(get_current_admin),db=Depends(get_db)):
    if body.enabled:
        async with db.execute('SELECT COUNT(*) FROM proxies') as cur:count=(await cur.fetchone())[0]
        if not count:raise HTTPException(status_code=400,detail='代理库为空，请先保存代理')
    await db.execute("UPDATE configs SET value=? WHERE key='proxy.enabled'",('true' if body.enabled else 'false',))
    await db.commit()
    await log_action(db,admin['id'],'proxy_toggle','proxies',None,'enabled' if body.enabled else 'disabled',request.client.host if request.client else '')
    return await get_proxies(db)
