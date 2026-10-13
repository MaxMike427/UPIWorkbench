"""V1.1.0 新功能与隐私边界，不调用真实外部服务。"""
import asyncio
import json

import aiosqlite
import httpx

from tests.test_product_requirements import client, _login
from app.services.qualification_service import CheckOutcome


def test_proxy_library_validates_redacts_and_preserves_when_disabled(client):
    h=_login(client)
    r=client.put('/api/proxies',headers=h,json={'text':'http://proxy.example:80\nsocks5://user:secret@proxy.example:1080','enabled':True})
    assert r.status_code==200,r.text
    result=client.get('/api/proxies',headers=h).json()
    assert result['enabled'] is True and result['total']==2
    assert 'secret' not in json.dumps(result)
    assert 'socks5' in json.dumps(result)
    r=client.put('/api/proxies',headers=h,json={'text':'http://good.example:80\nfile:///etc/passwd','enabled':True})
    assert r.status_code==400 and '第 2 行' in r.json()['detail']
    assert client.get('/api/proxies',headers=h).json()['total']==2
    assert client.put('/api/proxies/enabled',headers=h,json={'enabled':False}).status_code==200
    result=client.get('/api/proxies',headers=h).json()
    assert result['enabled'] is False and result['total']==2


def test_proxy_library_accepts_bare_host_port_as_http(client):
    h=_login(client)
    r=client.put('/api/proxies',headers=h,json={'text':'134.209.144.56:80','enabled':True})
    assert r.status_code==200,r.text
    result=client.get('/api/proxies',headers=h).json()
    assert result['items']==[{'id':1,'url':'http://134.209.144.56:80','protocol':'http','host':'134.209.144.56','port':80}]


def test_created_task_snapshots_proxy_and_sms_switch(client):
    h=_login(client)
    client.post('/api/credentials/import',headers=h,files={'file':('a.txt','a@example.com---pw---2FA---eyJoriginal')})
    client.put('/api/configs/sms.enabled',headers=h,json={'value':'false'})
    client.put('/api/proxies',headers=h,json={'text':'https://user:secret@proxy.example:8443','enabled':True})
    r=client.post('/api/batches',headers=h,json={'name':'快照','credential_ids':[1]})
    assert r.status_code==200,r.text
    detail=client.get('/api/batches/'+str(r.json()['id']),headers=h).json()
    assert detail['sms_enabled'] is False and detail['proxy_enabled'] is True
    assert 'secret' not in json.dumps(detail)
    async def check():
        async with aiosqlite.connect(client._product_db_path) as db:
            raw=(await (await db.execute('SELECT snapshot_json FROM task_batches WHERE id=?',(detail['id'],))).fetchone())[0]
            snapshot=json.loads(raw)
            assert snapshot['proxies']==['https://user:secret@proxy.example:8443']
            assert snapshot['sms_enabled'] is False
    asyncio.run(check())


def test_sms_disabled_and_no_proxy_blocks_new_payment_without_sms_requirement(client,monkeypatch):
    h=_login(client)
    client.post('/api/credentials/import',headers=h,files={'file':('a.txt','a@example.com---pw---2FA---eyJoriginal')})
    client.put('/api/configs/upi_provider.cdk',headers=h,json={'value':'ohk_test'})
    client.put('/api/configs/sms_provider.cdk',headers=h,json={'value':'sms_test'})
    client.put('/api/configs/sms.enabled',headers=h,json={'value':'false'})
    bid=client.post('/api/batches',headers=h,json={'name':'无代理','credential_ids':[1]}).json()['id']
    r=client.post(f'/api/batches/{bid}/start',headers=h)
    assert r.status_code==400 and '代理' in r.json()['detail']


def test_integration_settings_mask_key_and_template_validation(client):
    h=_login(client)
    for key,value in [('sub2api.base_url','https://sub2.example/api/v1/'),('sub2api.admin_key','admin-secret'),('sub2api.enabled','true')]:
        assert client.put('/api/configs/'+key,headers=h,json={'value':value}).status_code==200
    settings=client.get('/api/configs/workspace',headers=h).json()
    assert 'admin-secret' not in json.dumps(settings)
    template={'group_ids':[1],'concurrency':3,'rate_multiplier':1.2,'models':['gpt-5'],'update_existing':False}
    assert client.put('/api/integrations/sub2api/template',headers=h,json=template).status_code==200
    assert client.get('/api/integrations/sub2api/template',headers=h).json()['models']==['gpt-5']
    assert client.put('/api/integrations/sub2api/template',headers=h,json={**template,'concurrency':0}).status_code==422
    assert client.put('/api/configs/sub2api.base_url',headers=h,json={'value':'file:///etc/passwd'}).status_code==400


def test_sub2api_connection_uses_admin_header_and_only_reads(client,monkeypatch):
    h=_login(client)
    client.put('/api/configs/sub2api.base_url',headers=h,json={'value':'https://sub2.example/api/v1/'})
    client.put('/api/configs/sub2api.admin_key',headers=h,json={'value':'admin-secret'})
    calls=[]
    async def get(self,url,**kwargs):
        calls.append(url)
        assert kwargs['headers']['x-api-key']=='admin-secret'
        assert 'Authorization' not in kwargs['headers']
        return httpx.Response(200,json={'code':0,'data':{'items':[{'id':1,'name':'OpenAI','platform':'openai','status':'active'}],'total':1}},request=httpx.Request('GET',url))
    monkeypatch.setattr(httpx.AsyncClient,'get',get)
    r=client.post('/api/integrations/sub2api/test',headers=h)
    assert r.status_code==200,r.text
    assert calls==['https://sub2.example/api/v1/admin/groups']
    assert 'admin-secret' not in r.text


def test_sms_off_records_success_without_sms_and_disables_json_and_push(client,monkeypatch):
    from app.services import batch_service as bs
    from app.services.success_service import count_successes
    h=_login(client)
    client.post('/api/credentials/import',headers=h,files={'file':('a.txt','a@example.com---pw---2FA---eyJoriginal')})
    client.put('/api/configs/sms.enabled',headers=h,json={'value':'false'})
    class Qual:
        def __init__(self,*args,**kwargs):pass
        async def check_single(self,at):return CheckOutcome(True,'eligible',{})
    class UPI:
        def __init__(self,db,**kwargs):self.db=db
        async def create_with_retries(self,cid,*args,**kwargs):
            await self.db.execute("INSERT INTO upi_tasks(credential_id,amount,status,upi_link) VALUES(?,1,'success','https://pay.example')",(cid,));await self.db.commit()
            return {'id':1,'status':'success'}
    class Forbidden:
        def __init__(self,*args,**kwargs):raise AssertionError('接码关闭不能访问 handy')
    monkeypatch.setattr(bs,'QualificationService',Qual);monkeypatch.setattr(bs,'UPIService',UPI);monkeypatch.setattr(bs,'SMSService',Forbidden)
    async def run():
        svc=bs.BatchService(client._product_db_path)
        bid=await svc.create_batch('不接码',[1],100)
        async with aiosqlite.connect(client._product_db_path) as db:
            await db.execute("UPDATE task_batches SET status='running' WHERE id=?",(bid,));await db.commit()
        await svc.run(bid)
    asyncio.run(run())
    assert client.get('/api/credentials',headers=h).json()['items'][0]['status']=='paid_without_sms'
    successes=client.get('/api/successes',headers=h).json()
    assert successes['total']==1
    assert successes['items'][0]['status']=='paid_without_sms'
    assert client.get('/api/successes/export?ids=1',headers=h).text.strip()=='a@example.com----pw----2FA'
    assert client.get('/api/successes/download?ids=1&format=both',headers=h).status_code==409


async def test_ytc_external_proxy_is_sent_without_disclosing_password(monkeypatch):
    from app.services.upi_providers import YtcUPIProvider
    sent=[]
    async def post(self,url,**kwargs):
        sent.append(kwargs['json']);return httpx.Response(200,json={'id':'job'},request=httpx.Request('POST',url))
    monkeypatch.setattr(httpx.AsyncClient,'post',post)
    provider=YtcUPIProvider('https://ytc.example','ohk_test',proxies=['socks5://u:secret@p.example:1080'])
    async with httpx.AsyncClient() as c:await provider.create_job(c,'eyJoriginal',2)
    assert sent[0]['proxy_source']=='provided'
    assert sent[0]['proxies']=='socks5://u:secret@p.example:1080'


def test_integration_cannot_be_enabled_without_url_and_admin_key(client):
    h=_login(client)
    r=client.put('/api/configs/sub2api.enabled',headers=h,json={'value':'true'})
    assert r.status_code==400
    r=client.post('/api/configs/batch',headers=h,json={'items':{'sub2api.enabled':'true','sms.enabled':'false'}})
    assert r.status_code==400
    values={i['key']:i['value'] for i in client.get('/api/configs',headers=h).json()}
    assert values['sms.enabled']=='true' and values['sub2api.enabled']=='false'


def test_v110_version_file_matches_api(client):
    from pathlib import Path
    r=client.get('/api/health')
    assert r.json()['version']=='1.1.0'
    root=Path(__file__).parents[1]
    assert '1.1.0' in (root/'VERSION').read_text()


def test_generic_config_cannot_bypass_integration_template_validation(client):
    h=_login(client)
    before=client.get('/api/integrations/sub2api/template',headers=h).json()
    invalid=json.dumps({'concurrency':0,'models':[''],'group_ids':[-1]})
    r=client.put('/api/configs/sub2api.template',headers=h,json={'value':invalid})
    assert r.status_code==400
    assert client.get('/api/integrations/sub2api/template',headers=h).json()==before
    r=client.post('/api/configs/batch',headers=h,json={'items':{'sms.enabled':'false','sub2api.template':'not-json'}})
    assert r.status_code==400
    settings={i['key']:i['value'] for i in client.get('/api/configs/workspace',headers=h).json()}
    assert settings['sms.enabled']=='true'


def test_old_paid_task_resumes_sms_without_proxy_or_payment_cdk(client,monkeypatch):
    h=_login(client)
    client.post('/api/credentials/import',headers=h,files={'file':('a.txt','a@example.com---pw---2FA---eyJoriginal')})
    client.put('/api/configs/sms_provider.cdk',headers=h,json={'value':'SMS-test'})
    bid=client.post('/api/batches',headers=h,json={'name':'已付旧任务','credential_ids':[1]}).json()['id']
    async def seed():
        async with aiosqlite.connect(client._product_db_path) as db:
            await db.execute("UPDATE task_batches SET workflow_version=2,snapshot_json=NULL,status='paused' WHERE id=?",(bid,))
            await db.execute("UPDATE batch_members SET stage='paid' WHERE batch_id=?",(bid,));await db.commit()
    asyncio.run(seed())
    calls=[]
    monkeypatch.setattr('app.services.batch_service.BatchService.start',lambda self,bid:calls.append(bid))
    r=client.post(f'/api/batches/{bid}/resume',headers=h)
    assert r.status_code==200,r.text
    assert calls==[bid]


def test_old_pending_task_does_not_get_new_proxy_requirement(client,monkeypatch):
    h=_login(client)
    client.post('/api/credentials/import',headers=h,files={'file':('a.txt','a@example.com---pw---2FA---eyJoriginal')})
    client.put('/api/configs/upi_provider.cdk',headers=h,json={'value':'ohk_test'})
    client.put('/api/configs/sms_provider.cdk',headers=h,json={'value':'sms_test'})
    bid=client.post('/api/batches',headers=h,json={'name':'旧待支付任务','credential_ids':[1]}).json()['id']
    async def seed():
        async with aiosqlite.connect(client._product_db_path) as db:
            await db.execute("UPDATE task_batches SET workflow_version=1,snapshot_json=NULL,status='created' WHERE id=?",(bid,));await db.commit()
    asyncio.run(seed())
    calls=[]
    monkeypatch.setattr('app.services.batch_service.BatchService.start',lambda self,bid:calls.append(bid))
    r=client.post(f'/api/batches/{bid}/start',headers=h)
    assert r.status_code==200,r.text
    assert calls==[bid]


def test_sms_off_report_describes_no_sms_branch(client):
    h=_login(client)
    client.post('/api/credentials/import',headers=h,files={'file':('a.txt','a@example.com---pw---2FA---eyJoriginal')})
    async def seed():
        async with aiosqlite.connect(client._product_db_path) as db:
            await db.execute("INSERT INTO task_batches(name,total_credentials,status,current_step,progress_json,workflow_version,snapshot_json) VALUES('不接码报告',1,'completed','done','{}',3,?)",
                             (json.dumps({'sms_enabled':False}),))
            await db.execute("INSERT INTO upi_tasks(credential_id,amount,status,upi_link) VALUES(1,100,'success','https://pay.example')")
            await db.execute("INSERT INTO batch_members(batch_id,credential_id,stage,upi_task_id) VALUES(last_insert_rowid(),1,'paid_without_sms',last_insert_rowid())")
            await db.commit()
    asyncio.run(seed())
    r=client.get('/api/batches/1/report',headers=h)
    assert r.status_code==200
    assert '已成功未接码' in r.text and '自动接码绑定' not in r.text


def test_upi_api_does_not_echo_raw_proxy_passwords(client):
    h=_login(client)
    async def seed():
        async with aiosqlite.connect(client._product_db_path) as db:
            await db.execute("INSERT INTO upi_tasks(credential_id,amount,status,raw_response,error_msg) VALUES(1,100,'failed',?,?)",
                             (json.dumps({'settings':{'proxies':'socks5://user:proxy-secret@p.example:1080'}}),'proxy failure socks5://user:proxy-secret@p.example:1080'))
            await db.commit()
    asyncio.run(seed())
    for path in ('/api/upi/tasks','/api/upi/tasks/1'):
        r=client.get(path,headers=h)
        assert r.status_code==200 and 'proxy-secret' not in r.text


def test_legacy_ytc_create_api_cannot_bypass_disabled_proxy_library(client,monkeypatch):
    h=_login(client)
    client.post('/api/credentials/import',headers=h,files={'file':('a.txt','a@example.com---pw---2FA---eyJoriginal')})
    client.put('/api/configs/upi_provider.cdk',headers=h,json={'value':'ohk_test'})
    client.put('/api/configs/upi_provider.api_key',headers=h,json={'value':'ohk_test'})
    async def forbidden(*args,**kwargs):raise AssertionError('无代理时不能提交新单')
    monkeypatch.setattr(httpx.AsyncClient,'post',forbidden)
    r=client.post('/api/upi/create',headers=h,json={'credential_ids':[1],'amount':100,'retry_interval_s':0})
    assert r.status_code==400 and '代理' in r.json()['detail']


def test_manual_push_returns_each_selected_account_result_without_hiding_partial_success(client,monkeypatch):
    h=_login(client)
    client.post('/api/configs/batch',headers=h,json={'items':{'sub2api.enabled':'true','sub2api.base_url':'https://sub2.example','sub2api.admin_key':'admin-test'}})
    async def enqueue(db,cid,bid,snapshot):
        if cid==2:raise ValueError('未接码账号不能自动推送')
        return 11
    monkeypatch.setattr('app.services.integration_service.enqueue_push',enqueue)
    r=client.post('/api/integrations/sub2api/pushes',headers=h,json={'ids':[1,2]})
    assert r.status_code==200,r.text
    assert r.json()['count']==1 and r.json()['failed']==1
    assert r.json()['results']==[{'credential_id':1,'push_id':11,'status':'queued'},{'credential_id':2,'status':'rejected','error':'未接码账号不能自动推送'}]


def test_legacy_retry_keeps_sms_on_and_integration_off_despite_new_global_settings(client):
    from app.services.batch_service import BatchService
    h=_login(client)
    client.post('/api/credentials/import',headers=h,files={'file':('a.txt','a@example.com---pw---2FA---eyJoriginal')})
    bid=client.post('/api/batches',headers=h,json={'name':'旧接码失败','credential_ids':[1]}).json()['id']
    client.post('/api/configs/batch',headers=h,json={'items':{'sms.enabled':'false','sub2api.enabled':'true','sub2api.base_url':'https://sub2.example','sub2api.admin_key':'admin-test'}})
    async def run():
        async with aiosqlite.connect(client._product_db_path) as db:
            await db.execute("UPDATE task_batches SET workflow_version=2,snapshot_json=NULL,status='failed' WHERE id=?",(bid,))
            await db.execute("UPDATE batch_members SET stage='bind_failed' WHERE batch_id=?",(bid,));await db.commit()
        result=await BatchService(client._product_db_path).create_retry_batch(bid)
        return result['batch_id']
    retry=asyncio.run(run())
    detail=client.get(f'/api/batches/{retry}',headers=h).json()
    assert detail['sms_enabled'] is True and detail['integration_enabled'] is False
