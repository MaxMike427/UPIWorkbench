"""sub2api Admin Key 集成：持久推送队列、官方 Codex 导入、超时人工核对。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import aiosqlite
import httpx

from ..config import get_settings
from ..models import IntegrationTemplate
from .success_service import SUCCESS_FILTER


def normalize_url(value:str)->str:
    value=value.strip().rstrip('/')
    try:
        url=urlsplit(value)
        if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError()
        url.port
        if any(ord(c)<33 for c in value):raise ValueError()
    except ValueError:
        raise ValueError('请输入有效的 HTTP/HTTPS sub2api 实例 URL，不要包含账号密码或查询参数')
    path=url.path.rstrip('/')
    if path.endswith('/api/v1'):path=path[:-7]
    return urlunsplit((url.scheme,url.netloc,path,'','')).rstrip('/')


async def settings(db)->dict:
    async with db.execute("SELECT key,value FROM configs WHERE key LIKE 'sub2api.%'") as cur:
        values={r['key']:r['value'] async for r in cur}
    return {'enabled':values.get('sub2api.enabled')=='true','url':values.get('sub2api.base_url',''),
            'key':values.get('sub2api.admin_key',''),'template':json.loads(values.get('sub2api.template') or '{}')}


async def read_groups(url:str,key:str)->list:
    if not key:raise ValueError('请先保存 sub2api 管理员 Admin Key')
    endpoint=normalize_url(url)+'/api/v1/admin/groups'
    items=[]
    async with httpx.AsyncClient(follow_redirects=False,timeout=30.0) as client:
        for page in range(1,101):
            try:r=await client.get(endpoint,params={'page':page,'page_size':100},headers={'x-api-key':key})
            except httpx.RequestError:raise ValueError('sub2api 连接失败，请检查 URL 与网络')
            data=unwrap(r)
            rows=data if isinstance(data,list) else data.get('items',[])
            items.extend({k:row.get(k) for k in ('id','name','platform','status')} for row in rows if row.get('platform')=='openai')
            if isinstance(data,list) or len(rows)<100 or page*100>=int(data.get('total',len(rows))):break
    return items


def unwrap(response)->dict|list:
    if response.status_code in (401,403):raise ValueError('sub2api Admin Key 无效或管理员权限不足')
    if response.status_code>=400 or response.status_code<200:raise ValueError(f'sub2api 请求失败（HTTP {response.status_code}）')
    if 300<=response.status_code<400:raise ValueError('sub2api 返回重定向，请填写最终实例 URL')
    try:body=response.json()
    except ValueError:raise ValueError('sub2api 返回非 JSON，请检查实例 URL')
    if not isinstance(body,(dict,list)):raise ValueError('sub2api 响应格式无效')
    if isinstance(body,dict) and 'code' in body and body['code'] not in (0,200):raise ValueError('sub2api 返回业务错误，请检查配置与请求权限')
    return body.get('data',body) if isinstance(body,dict) else body


def extract_oauth(obj)->dict:
    """只提取真实 access/refresh/id token，不提交整包账户/分组数据。"""
    candidates=[]
    def walk(o):
        if isinstance(o,dict):
            at=o.get('access_token') or o.get('accessToken')
            if isinstance(at,str) and at:
                result={'access_token':at}
                for key,aliases in {'refresh_token':('refresh_token','refreshToken'),'id_token':('id_token','idToken'),
                                    'email':('email',),'account_id':('account_id','chatgpt_account_id'),
                                    'chatgpt_user_id':('chatgpt_user_id','user_id'),'organization_id':('organization_id','org_id'),'plan_type':('plan_type',)}.items():
                    value=next((o[a] for a in aliases if isinstance(o.get(a),str)),None)
                    if value:result[key]=value
                candidates.append(result)
            for v in o.values():walk(v)
        elif isinstance(o,list):
            for v in o:walk(v)
    walk(obj)
    if len(candidates)!=1:raise ValueError('账号 JSON 需要恰好一个有效 OAuth 凭据对象')
    result=candidates[0]
    # 身份字段用于重复核对，解析 JWT 不等于验证 token 有效性，远端仍会执行正式导入校验。
    try:
        encoded=result['access_token'].split('.')[1]
        claims=json.loads(base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4)))
        auth=claims.get('https://api.openai.com/auth') or {}
        for key,value in {'account_id':auth.get('chatgpt_account_id'),
                          'chatgpt_user_id':auth.get('chatgpt_user_id') or auth.get('user_id') or claims.get('sub'),
                          'email':claims.get('email')}.items():
            if key not in result and isinstance(value,str) and value:result[key]=value
    except (ValueError,IndexError,TypeError,AttributeError):pass
    return result


async def account_payload(db,cid:int)->dict:
    async with db.execute('SELECT paths_json FROM account_exports WHERE credential_id=? AND format=\'cpa\' ORDER BY id DESC LIMIT 1',(cid,)) as cur:row=await cur.fetchone()
    if not row:raise ValueError('缺少接码后 CPA/OAuth JSON，不推送旧 AT 或未接码账号')
    root=get_settings().data_dir.resolve()/'account_exports'
    candidates=[]
    for stored in json.loads(row['paths_json']):
        p=Path(stored).resolve()
        if not p.is_relative_to(root) or not p.is_file():raise ValueError('本地账号 JSON 不可用')
        if p.stat().st_size>5*1024*1024:raise ValueError('账号 JSON 过大')
        try:candidates.append(extract_oauth(json.loads(p.read_text(encoding='utf-8-sig'))))
        except ValueError:continue
    if len(candidates)!=1:raise ValueError('未找到唯一可推送账号凭据')
    if not candidates[0].get('refresh_token'):raise ValueError('缺少 Refresh Token，当前账号不能推送，需要接码后的完整 OAuth 账号')
    return candidates[0]


async def find_remote_account(url:str,key:str,payload:dict)->int|None:
    """只读检查确切 token 身份，不以邮箱近似匹配而覆盖其他账号。"""
    async with httpx.AsyncClient(follow_redirects=False,timeout=30.0) as client:
        for page in range(1,101):
            try:r=await client.get(url+'/api/v1/admin/accounts',params={'platform':'openai','type':'oauth','page':page,'page_size':100,'search':payload.get('email','')},headers={'x-api-key':key})
            except httpx.RequestError:raise ValueError('无法核对远端已有账号，停止推送')
            data=unwrap(r)
            rows=data.get('items',[]) if isinstance(data,dict) else data
            for row in rows:
                credentials=row.get('credentials') or {}
                if (credentials.get('access_token')==payload['access_token'] or
                    (payload.get('refresh_token') and credentials.get('refresh_token')==payload['refresh_token']) or
                    (payload.get('account_id') and payload.get('chatgpt_user_id') and
                     credentials.get('chatgpt_account_id')==payload['account_id'] and credentials.get('chatgpt_user_id')==payload['chatgpt_user_id'])):
                    return row['id']
            if len(rows)<100 or not isinstance(data,dict) or page*100>=data.get('total',len(rows)):return None
    raise ValueError('远端账号过多，未完成重复核对，停止推送')


async def enqueue_push(db,cid:int,bid:int|None,snapshot:dict)->int|None:
    if not snapshot.get('integration_enabled'):return None
    async with db.execute(f'SELECT c.id FROM credentials c WHERE c.id=? AND {SUCCESS_FILTER}',(cid,)) as cur:
        if not await cur.fetchone():raise ValueError('只有本地全流程成功账号可推送')
    async with db.execute("SELECT 1 FROM batch_members WHERE credential_id=? AND stage='done' AND sms_task_id IS NOT NULL LIMIT 1",(cid,)) as cur:
        if not await cur.fetchone():raise ValueError('未接码账号不能推送')
    url=normalize_url(snapshot.get('integration_url',''))
    template=IntegrationTemplate(**snapshot.get('integration_template',{})).model_dump()
    async with db.execute("SELECT id,status FROM integration_pushes WHERE instance_url=? AND credential_id=?"
                          " AND status IN ('pending','pushing','needs_review','succeeded','skipped') ORDER BY id DESC",(url,cid)) as cur:existing=await cur.fetchall()
    for row in existing:
        if row['status'] in ('pending','pushing','needs_review') or not template['update_existing']:
            return row['id']
    status,error='pending',''
    try:token=(await account_payload(db,cid))['access_token']
    except ValueError as e:
        status,error='failed',str(e)
        async with db.execute("SELECT new_access_token FROM sms_tasks WHERE credential_id=? AND status='bound' ORDER BY id DESC LIMIT 1",(cid,)) as cur:row=await cur.fetchone()
        token=row['new_access_token'] if row and row['new_access_token'] else f'unavailable:{cid}:{bid}'
    fingerprint=hashlib.sha256(token.encode()).hexdigest()
    await db.execute('INSERT OR IGNORE INTO integration_pushes(credential_id,batch_id,instance_url,fingerprint,template_json,status,error_msg) VALUES(?,?,?,?,?,?,?)',
                     (cid,bid,url,fingerprint,json.dumps(template),status,error))
    await db.commit()
    async with db.execute('SELECT id FROM integration_pushes WHERE instance_url=? AND credential_id=? AND fingerprint=?',(url,cid,fingerprint)) as cur:
        return (await cur.fetchone())['id']


async def process_push(db,push_id:int)->None:
    config=await settings(db)
    if not config['enabled'] or not config['url'] or not config['key']:return
    async with db.execute('SELECT * FROM integration_pushes WHERE id=?',(push_id,)) as cur:row=await cur.fetchone()
    if not row or row['status']!='pending':return
    if row['next_attempt_at'] and time.time()<row['next_attempt_at']:return
    if normalize_url(config['url'])!=row['instance_url']:return
    async with db.execute("UPDATE integration_pushes SET status='pushing',attempt_count=attempt_count+1,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",(push_id,)) as cur:
        claimed=cur.rowcount
    await db.commit()
    if not claimed:return
    final='failed';error='';remote_id=None
    try:
        payload=await account_payload(db,row['credential_id'])
        if hashlib.sha256(payload['access_token'].encode()).hexdigest()!=row['fingerprint']:
            raise ValueError('本地凭据已变化，请重新加入推送队列')
        template=IntegrationTemplate(**json.loads(row['template_json'])).model_dump()
        groups=await read_groups(row['instance_url'],config['key'])
        valid={g['id'] for g in groups if g.get('status')=='active'}
        if any(g not in valid for g in template['group_ids']):raise ValueError('选中的 OpenAI 分组不存在或未启用')
        if not template['update_existing']:
            remote_id=await find_remote_account(row['instance_url'],config['key'],payload)
            if remote_id:
                await db.execute("UPDATE integration_pushes SET status='skipped',remote_account_id=?,error_msg='',updated_at=CURRENT_TIMESTAMP WHERE id=?",(remote_id,push_id));await db.commit()
                return
        body={'content':json.dumps(payload),'group_ids':template['group_ids'],'concurrency':template['concurrency'],
              'rate_multiplier':template['rate_multiplier'],'update_existing':template['update_existing'],
              'skip_default_group_bind':not template['group_ids']}
        if template['models']:
            # 官方导入支持 credential_extras，账号模型映射即为白名单，不修改共享分组。
            body['credential_extras']={'model_mapping':{m:m for m in template['models']}}
        async with httpx.AsyncClient(follow_redirects=False,timeout=60.0) as client:
            try:
                response=await client.post(row['instance_url']+'/api/v1/admin/accounts/import/codex-session',json=body,
                                           headers={'x-api-key':config['key'],'Idempotency-Key':f'upi-workbench-{push_id}-{row["fingerprint"]}'})
            except httpx.RequestError:
                final='needs_review';raise ValueError('推送请求结果不确定，先核对远端，不能直接重建账号')
            if response.status_code>=500:
                final='needs_review';raise ValueError('远端服务异常，账号可能已写入，请先核对')
            if response.status_code==429:
                try:delay=max(1,int(response.headers.get('Retry-After','60')))
                except ValueError:delay=60
                await db.execute("UPDATE integration_pushes SET status='pending',error_msg='远端限流，按 Retry-After 等待',next_attempt_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(time.time()+delay,push_id));await db.commit()
                return
            if 200<=response.status_code<300:
                final='needs_review'  # 成功 HTTP 但业务结果缺失，远端可能已写入。
            result=unwrap(response)
        if not isinstance(result,dict):raise ValueError('远端导入响应无效')
        item=next((i for i in result.get('items',[]) if i.get('index')==1),None)
        if item and item.get('action') in ('created','updated','skipped') and not result.get('failed',0):
            final='skipped' if item['action']=='skipped' else 'succeeded';remote_id=item.get('account_id')
        else:
            if item and item.get('action')=='failed':final='failed'
            raise ValueError('远端未确认账号导入成功，请检查集成配置')
    except ValueError as e:error=str(e)
    except Exception:error='集成操作异常，请检查远端配置'
    await db.execute('UPDATE integration_pushes SET status=?,remote_account_id=?,error_msg=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                     (final,remote_id,error[:300],push_id));await db.commit()


async def reconcile_push(db,pid:int)->dict:
    config=await settings(db)
    async with db.execute('SELECT * FROM integration_pushes WHERE id=?',(pid,)) as cur:row=await cur.fetchone()
    if not row or row['status']!='needs_review':raise ValueError('仅待核对记录可查询远端')
    if normalize_url(config['url'])!=row['instance_url']:raise ValueError('当前实例与推送记录不一致')
    payload=await account_payload(db,row['credential_id'])
    rid=await find_remote_account(row['instance_url'],config['key'],payload)
    if not rid:raise ValueError('未找到确切凭据匹配，继续保留需核对状态，请人工确认远端')
    await db.execute("UPDATE integration_pushes SET status='succeeded',remote_account_id=?,error_msg='',updated_at=CURRENT_TIMESTAMP WHERE id=?",(rid,pid));await db.commit()
    return {'status':'succeeded','remote_account_id':rid}


async def recover_pushes(db):
    await db.execute("UPDATE integration_pushes SET status='needs_review',error_msg='服务重启前推送结果未确认，请核对远端' WHERE status='pushing'")
    await db.commit()


async def push_worker(db_path):
    while True:
        try:
            async with aiosqlite.connect(db_path) as db:
                db.row_factory=aiosqlite.Row
                config=await settings(db)
                if config['enabled'] and config['url'] and config['key']:
                    async with db.execute("SELECT id FROM integration_pushes WHERE status='pending' AND instance_url=?"
                                          " AND COALESCE(next_attempt_at,0)<=? ORDER BY id LIMIT 10",(normalize_url(config['url']),time.time())) as cur:ids=[r['id'] async for r in cur]
                    for pid in ids:await process_push(db,pid)
        except asyncio.CancelledError:raise
        except Exception:pass
        await asyncio.sleep(3)
