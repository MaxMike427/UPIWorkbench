"""sub2api 只读连接测试、模板与持久推送队列。"""
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..auth import get_current_admin
from ..database import get_db,log_action
from ..models import IntegrationTemplate, BatchDeleteRequest
from ..services import integration_service as service

router=APIRouter(prefix='/api/integrations/sub2api',tags=['integrations'])


@router.get('/template')
async def get_template(admin:dict=Depends(get_current_admin),db=Depends(get_db)):
    return (await service.settings(db))['template']


@router.put('/template')
async def set_template(body:IntegrationTemplate,request:Request,admin:dict=Depends(get_current_admin),db=Depends(get_db)):
    if any(i<=0 for i in body.group_ids) or any(not m.strip() or len(m)>100 for m in body.models):
        raise HTTPException(status_code=400,detail='分组编号或模型名称无效')
    value=body.model_dump();value['group_ids']=list(dict.fromkeys(value['group_ids']));value['models']=list(dict.fromkeys(m.strip() for m in value['models']))
    await db.execute("UPDATE configs SET value=? WHERE key='sub2api.template'",(json.dumps(value),));await db.commit()
    await log_action(db,admin['id'],'integration_template','integration',None,'',request.client.host if request.client else '')
    return value


@router.get('/groups')
async def groups(admin:dict=Depends(get_current_admin),db=Depends(get_db)):
    config=await service.settings(db)
    try:return {'items':await service.read_groups(config['url'],config['key'])}
    except ValueError as e:raise HTTPException(status_code=400,detail=str(e))


@router.post('/test')
async def test_connection(admin:dict=Depends(get_current_admin),db=Depends(get_db)):
    result=await groups(admin,db)
    return {'ok':True,'group_count':len(result['items'])}


@router.get('/pushes')
async def pushes(page:int=Query(default=1,ge=1),admin:dict=Depends(get_current_admin),db=Depends(get_db)):
    async with db.execute('SELECT COUNT(*) AS c FROM integration_pushes') as cur:total=(await cur.fetchone())['c']
    async with db.execute('SELECT p.id,p.credential_id,c.account,p.instance_url,p.status,p.attempt_count,p.remote_account_id,p.error_msg,p.updated_at'
                          ' FROM integration_pushes p LEFT JOIN credentials c ON c.id=p.credential_id ORDER BY p.id DESC LIMIT 20 OFFSET ?',((page-1)*20,)) as cur:items=[dict(r) async for r in cur]
    return {'items':items,'total':total,'page':page,'page_size':20}


@router.post('/pushes')
async def enqueue(body:BatchDeleteRequest,admin:dict=Depends(get_current_admin),db=Depends(get_db)):
    config=await service.settings(db)
    if not config['enabled']:raise HTTPException(status_code=400,detail='请在设置中启用 sub2api 集成')
    snapshot={'integration_enabled':True,'integration_url':config['url'],'integration_template':config['template']}
    ids=[];results=[]
    for cid in list(dict.fromkeys(body.ids)):
        try:
            pid=await service.enqueue_push(db,cid,None,snapshot);ids.append(pid)
            results.append({'credential_id':cid,'push_id':pid,'status':'queued'})
        except ValueError as e:results.append({'credential_id':cid,'status':'rejected','error':str(e)})
    return {'ids':ids,'count':len(ids),'failed':len(results)-len(ids),'results':results}


@router.post('/pushes/{pid}/retry')
async def retry_push(pid:int,admin:dict=Depends(get_current_admin),db=Depends(get_db)):
    async with db.execute('SELECT status FROM integration_pushes WHERE id=?',(pid,)) as cur:row=await cur.fetchone()
    if not row:raise HTTPException(status_code=404,detail='推送记录不存在')
    if row['status']=='needs_review':raise HTTPException(status_code=409,detail='远端可能已写入，需先在 sub2api 核对，不可盲目重推')
    if row['status']!='failed':raise HTTPException(status_code=409,detail='仅失败推送可重试')
    await db.execute("UPDATE integration_pushes SET status='pending',error_msg='' WHERE id=?",(pid,));await db.commit()
    return {'ok':True}


@router.post('/pushes/{pid}/reconcile')
async def reconcile(pid:int,admin:dict=Depends(get_current_admin),db=Depends(get_db)):
    try:return await service.reconcile_push(db,pid)
    except ValueError as e:raise HTTPException(status_code=400,detail=str(e))
