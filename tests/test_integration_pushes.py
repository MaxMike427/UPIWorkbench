import io
import json
import zipfile

import aiosqlite
import httpx
import pytest

from app.database import init_db
from app.services.account_export_service import save_account_exports
from app.services.integration_service import enqueue_push,process_push,recover_pushes


@pytest.fixture()
async def db(tmp_path):
    path=str(tmp_path/'integration.sqlite')
    await init_db(path)
    async with aiosqlite.connect(path) as conn:
        conn.row_factory=aiosqlite.Row
        await conn.execute("INSERT INTO credentials(account,password,access_token) VALUES('a@example.com','pw','eyJnew')")
        await conn.execute("INSERT INTO task_batches(name,status,workflow_version) VALUES('done','completed',3)")
        await conn.execute("INSERT INTO sms_tasks(credential_id,status,new_access_token) VALUES(1,'bound','eyJnew')")
        await conn.execute("INSERT INTO upi_tasks(credential_id,amount,status,upi_link) VALUES(1,1,'success','https://pay.example')")
        await conn.execute("INSERT INTO batch_members(batch_id,credential_id,stage,upi_task_id,sms_task_id) VALUES(1,1,'done',1,1)")
        for key,value in [('sub2api.enabled','true'),('sub2api.base_url','https://sub2.example'),('sub2api.admin_key','admin-secret')]:
            await conn.execute('UPDATE configs SET value=? WHERE key=?',(value,key))
        await conn.commit()
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as z:z.writestr('auth.json',json.dumps({'email':'a@example.com','access_token':'eyJnew','refresh_token':'rt.synthetic-refresh'}))
        await save_account_exports(conn,1,1,{'cpa':buf.getvalue(),'sub2':buf.getvalue()})
        yield conn


def snapshot():
    return {'integration_enabled':True,'integration_url':'https://sub2.example','integration_template':{
        'group_ids':[7],'concurrency':4,'rate_multiplier':1.5,'models':['gpt-5'],'update_existing':False}}


async def test_push_maps_real_oauth_template_admin_key_and_deduplicates(db,monkeypatch):
    calls=[]
    async def get(self,url,**kwargs):
        if url.endswith('/groups'):body={'items':[{'id':7,'platform':'openai','status':'active'}],'total':1}
        else:body={'items':[],'total':0}
        return httpx.Response(200,json={'code':0,'data':body},request=httpx.Request('GET',url))
    async def post(self,url,**kwargs):
        calls.append(kwargs)
        assert kwargs['headers']['x-api-key']=='admin-secret'
        assert kwargs['json']['group_ids']==[7]
        assert kwargs['json']['concurrency']==4 and kwargs['json']['rate_multiplier']==1.5
        assert kwargs['json']['credential_extras']=={'model_mapping':{'gpt-5':'gpt-5'}}
        assert json.loads(kwargs['json']['content'])['refresh_token']=='rt.synthetic-refresh'
        return httpx.Response(200,json={'code':0,'data':{'created':1,'failed':0,'items':[{'index':1,'action':'created','account_id':21}]}},request=httpx.Request('POST',url))
    monkeypatch.setattr(httpx.AsyncClient,'get',get);monkeypatch.setattr(httpx.AsyncClient,'post',post)
    pid=await enqueue_push(db,1,1,snapshot())
    assert await enqueue_push(db,1,1,snapshot())==pid
    await process_push(db,pid)
    await process_push(db,pid)
    row=await (await db.execute('SELECT * FROM integration_pushes WHERE id=?',(pid,))).fetchone()
    assert row['status']=='succeeded' and row['remote_account_id']==21
    assert len(calls)==1


async def test_timeout_marks_needs_review_never_blindly_reposts(db,monkeypatch):
    async def get(self,url,**kwargs):
        body={'items':[{'id':7,'platform':'openai','status':'active'}],'total':1} if url.endswith('/groups') else {'items':[],'total':0}
        return httpx.Response(200,json={'code':0,'data':body},request=httpx.Request('GET',url))
    calls=[]
    async def post(self,url,**kwargs):calls.append(1);raise httpx.ReadTimeout('do-not-log-token')
    monkeypatch.setattr(httpx.AsyncClient,'get',get);monkeypatch.setattr(httpx.AsyncClient,'post',post)
    pid=await enqueue_push(db,1,1,snapshot())
    await process_push(db,pid);await process_push(db,pid)
    row=await (await db.execute('SELECT status,error_msg FROM integration_pushes WHERE id=?',(pid,))).fetchone()
    assert row['status']=='needs_review' and 'do-not-log' not in row['error_msg']
    assert len(calls)==1


async def test_existing_remote_account_is_skipped_without_overwrite_when_template_disabled(db,monkeypatch):
    async def get(self,url,**kwargs):
        if url.endswith('/groups'):body={'items':[{'id':7,'platform':'openai','status':'active'}],'total':1}
        else:body={'items':[{'id':44,'name':'a@example.com','credentials':{'email':'a@example.com','refresh_token':'rt.synthetic-refresh'}}],'total':1}
        return httpx.Response(200,json={'code':0,'data':body},request=httpx.Request('GET',url))
    async def post(*args,**kwargs):raise AssertionError('默认不能覆盖或重复创建已有账号')
    monkeypatch.setattr(httpx.AsyncClient,'get',get);monkeypatch.setattr(httpx.AsyncClient,'post',post)
    pid=await enqueue_push(db,1,1,snapshot())
    await process_push(db,pid)
    row=await (await db.execute('SELECT status,remote_account_id FROM integration_pushes WHERE id=?',(pid,))).fetchone()
    assert row['status']=='skipped' and row['remote_account_id']==44


async def test_disabled_integration_never_makes_requests_and_restart_flags_uncertain(db,monkeypatch):
    pid=await enqueue_push(db,1,1,snapshot())
    await db.execute("UPDATE configs SET value='false' WHERE key='sub2api.enabled'");await db.commit()
    async def forbidden(*args,**kwargs):raise AssertionError('关闭集成不能调用远端')
    monkeypatch.setattr(httpx.AsyncClient,'get',forbidden);monkeypatch.setattr(httpx.AsyncClient,'post',forbidden)
    await process_push(db,pid)
    assert (await (await db.execute('SELECT status FROM integration_pushes WHERE id=?',(pid,))).fetchone())[0]=='pending'
    await db.execute("UPDATE integration_pushes SET status='pushing' WHERE id=?",(pid,));await db.commit()
    await recover_pushes(db)
    assert (await (await db.execute('SELECT status FROM integration_pushes WHERE id=?',(pid,))).fetchone())[0]=='needs_review'


async def test_429_waits_retry_after_and_does_not_spin(db,monkeypatch):
    async def get(self,url,**kwargs):
        body={'items':[{'id':7,'platform':'openai','status':'active'}],'total':1} if url.endswith('/groups') else {'items':[],'total':0}
        return httpx.Response(200,json={'code':0,'data':body},request=httpx.Request('GET',url))
    calls=[]
    async def post(self,url,**kwargs):
        calls.append(1)
        return httpx.Response(429,json={'message':'too many requests'},headers={'Retry-After':'90'},request=httpx.Request('POST',url))
    monkeypatch.setattr(httpx.AsyncClient,'get',get);monkeypatch.setattr(httpx.AsyncClient,'post',post)
    pid=await enqueue_push(db,1,1,snapshot())
    await process_push(db,pid);await process_push(db,pid)
    row=await (await db.execute('SELECT status,next_attempt_at FROM integration_pushes WHERE id=?',(pid,))).fetchone()
    assert row['status']=='pending' and row['next_attempt_at']>0
    assert len(calls)==1


async def test_needs_review_can_only_be_resolved_by_exact_remote_read(db,monkeypatch):
    from app.services.integration_service import reconcile_push
    pid=await enqueue_push(db,1,1,snapshot())
    await db.execute("UPDATE integration_pushes SET status='needs_review' WHERE id=?",(pid,));await db.commit()
    async def get(self,url,**kwargs):
        body={'items':[{'id':55,'credentials':{'access_token':'eyJnew'}}],'total':1}
        return httpx.Response(200,json={'code':0,'data':body},request=httpx.Request('GET',url))
    monkeypatch.setattr(httpx.AsyncClient,'get',get)
    result=await reconcile_push(db,pid)
    assert result['status']=='succeeded' and result['remote_account_id']==55


async def test_unconfirmed_success_response_requires_review_not_repost(db,monkeypatch):
    async def get(self,url,**kwargs):
        body={'items':[{'id':7,'platform':'openai','status':'active'}],'total':1} if url.endswith('/groups') else {'items':[],'total':0}
        return httpx.Response(200,json={'code':0,'data':body},request=httpx.Request('GET',url))
    calls=[]
    async def post(self,url,**kwargs):
        calls.append(1)
        return httpx.Response(200,json={'code':0,'data':{}},request=httpx.Request('POST',url))
    monkeypatch.setattr(httpx.AsyncClient,'get',get);monkeypatch.setattr(httpx.AsyncClient,'post',post)
    pid=await enqueue_push(db,1,1,snapshot())
    await process_push(db,pid);await process_push(db,pid)
    row=await (await db.execute('SELECT status FROM integration_pushes WHERE id=?',(pid,))).fetchone()
    assert row['status']=='needs_review' and len(calls)==1


async def test_missing_oauth_artifact_is_persisted_as_push_failure_not_local_failure(db):
    await db.execute('DELETE FROM account_exports WHERE credential_id=1');await db.commit()
    pid=await enqueue_push(db,1,1,snapshot())
    row=await (await db.execute('SELECT status,error_msg FROM integration_pushes WHERE id=?',(pid,))).fetchone()
    assert row['status']=='failed' and 'JSON' in row['error_msg']
    assert (await (await db.execute('SELECT stage FROM batch_members WHERE credential_id=1')).fetchone())[0]=='done'


async def test_default_push_does_not_create_new_queue_identity_when_token_rotates(db):
    from pathlib import Path
    pid=await enqueue_push(db,1,1,snapshot())
    await db.execute("UPDATE integration_pushes SET status='succeeded',remote_account_id=21 WHERE id=?",(pid,));await db.commit()
    row=await (await db.execute("SELECT paths_json FROM account_exports WHERE format='cpa'")).fetchone()
    Path(json.loads(row['paths_json'])[0]).write_text(json.dumps({'access_token':'eyJrotated','refresh_token':'rt.rotated'}))
    assert await enqueue_push(db,1,1,snapshot())==pid
    assert (await (await db.execute('SELECT COUNT(*) FROM integration_pushes')).fetchone())[0]==1


async def test_delayed_or_other_instance_pushes_do_not_starve_ready_queue(db,monkeypatch):
    import asyncio
    import time
    from app.services import integration_service as service
    for i in range(10):
        await db.execute('INSERT INTO integration_pushes(credential_id,instance_url,fingerprint,template_json,next_attempt_at) VALUES(1,?,?,?,?)',
                         ('https://other.example',str(i),'{}',time.time()+600))
    await db.commit()
    ready=await enqueue_push(db,1,1,snapshot())
    calls=[]
    async def process(conn,pid):calls.append(pid);raise asyncio.CancelledError()
    monkeypatch.setattr(service,'process_push',process)
    path=(await (await db.execute('PRAGMA database_list')).fetchone())[2]
    with pytest.raises(asyncio.CancelledError):await service.push_worker(path)
    assert calls==[ready]


async def test_rotated_remote_tokens_are_matched_by_both_user_and_account_identity(db,monkeypatch):
    from pathlib import Path
    row=await (await db.execute("SELECT paths_json FROM account_exports WHERE format='cpa'")).fetchone()
    Path(json.loads(row['paths_json'])[0]).write_text(json.dumps({'access_token':'eyJnew','refresh_token':'rt.synthetic-refresh','account_id':'workspace-1','chatgpt_user_id':'user-1'}))
    async def get(self,url,**kwargs):
        body={'items':[{'id':7,'platform':'openai','status':'active'}],'total':1} if url.endswith('/groups') else {
            'items':[{'id':44,'credentials':{'access_token':'rotated','refresh_token':'rotated','chatgpt_account_id':'workspace-1','chatgpt_user_id':'user-1'}}],'total':1}
        return httpx.Response(200,json={'code':0,'data':body},request=httpx.Request('GET',url))
    async def post(*args,**kwargs):raise AssertionError('已存在的同一账号不能重复导入')
    monkeypatch.setattr(httpx.AsyncClient,'get',get);monkeypatch.setattr(httpx.AsyncClient,'post',post)
    pid=await enqueue_push(db,1,1,snapshot())
    await process_push(db,pid)
    row=await (await db.execute('SELECT status,remote_account_id FROM integration_pushes WHERE id=?',(pid,))).fetchone()
    assert row['status']=='skipped' and row['remote_account_id']==44
