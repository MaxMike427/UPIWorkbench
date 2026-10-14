"""V1.1.0 新功能与隐私边界，不调用真实外部服务。"""
import asyncio
import json
import ssl
from pathlib import Path

import aiosqlite
import httpx
import pytest

from tests.test_product_requirements import client, _login
from app.services import proxy_service
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
    assert result['items']==[{'id':1,'url':'http://134.209.144.56:80','protocol':'http','host':'134.209.144.56','port':80,'is_dynamic':False}]


def test_proxy_library_supports_add_test_and_batch_delete(client,monkeypatch):
    h=_login(client)
    assert client.post('/api/proxies',headers=h,json={'text':'134.209.144.56:80','enabled':False}).status_code==200
    assert client.post('/api/proxies',headers=h,json={'text':'https://proxy.example:443','enabled':False}).status_code==200
    async def post(self,url,**kwargs):
        raise AssertionError('代理测试不应调用任何第三方测试接口')
    monkeypatch.setattr(httpx.AsyncClient,'post',post)
    monkeypatch.setattr(httpx.AsyncClient,'get',fake_probe({'ip':'203.0.113.9'}))
    result=client.post('/api/proxies/1/test',headers=h).json()
    assert result['ok'] is True and result['protocol']=='http' and isinstance(result['latency_ms'],int)
    assert result['exit_ip']=='203.0.113.9'
    assert client.post('/api/proxies/batch-delete',headers=h,json={'ids':[1]}).json()['deleted']==1
    assert [p['id'] for p in client.get('/api/proxies',headers=h).json()['items']]==[2]


def test_proxy_test_probes_locally_and_never_leaks_credentials(client,monkeypatch):
    h=_login(client)
    assert client.post('/api/proxies',headers=h,json={'text':'socks5://user:secret@proxy.example:1080','enabled':False}).status_code==200
    used=[]
    async def get(self,url,**kwargs):
        return httpx.Response(200,json={'ip':'203.0.113.10'},request=httpx.Request('GET',url))
    async def post(self,url,**kwargs):
        raise AssertionError('代理测试不得把代理地址或凭证发送给第三方测试接口')
    monkeypatch.setattr(httpx.AsyncClient,'get',get);monkeypatch.setattr(httpx.AsyncClient,'post',post)
    monkeypatch.setattr(proxy_service,'_client_for',scheme_recorder(used))
    result=client.post('/api/proxies/1/test',headers=h).json()
    assert result['ok'] is True and result['exit_ip']=='203.0.113.10'
    assert used==['socks5']
    assert 'secret' not in json.dumps(result,ensure_ascii=False)
    assert client.get('/api/proxies',headers=h).json()['items'][0]['url']=='socks5://****:****@proxy.example:1080'


def test_proxy_test_falls_back_to_http_for_tls_proxy_scheme(client,monkeypatch):
    h=_login(client)
    assert client.post('/api/proxies',headers=h,json={'text':'https://proxy.example:443','enabled':False}).status_code==200
    used=[]
    async def get(self,url,**kwargs):
        if current_scheme()=='https':raise httpx.ConnectError('tls handshake failed')
        return httpx.Response(200,json={'ip':'203.0.113.11'},request=httpx.Request('GET',url))
    def current_scheme():return used[-1] if used else ''
    monkeypatch.setattr(httpx.AsyncClient,'get',get)
    monkeypatch.setattr(proxy_service,'_client_for',scheme_recorder(used))
    result=client.post('/api/proxies/1/test',headers=h).json()
    assert used==['https','http']
    assert result['ok'] is True and result['protocol']=='https' and result['tested_protocol']=='http'


def test_proxy_test_reports_tls_interception_as_failure(client,monkeypatch):
    h=_login(client)
    client.post('/api/proxies',headers=h,json={'text':'134.209.144.56:80','enabled':False})
    async def get(self,url,**kwargs):
        error=ssl.SSLCertVerificationError('certificate verify failed')
        raise httpx.ConnectError('tls failed') from error
    monkeypatch.setattr(httpx.AsyncClient,'get',get)
    result=client.post('/api/proxies/1/test',headers=h).json()
    assert result['ok'] is False and 'TLS' in result['error']


def test_proxy_batch_test_runs_concurrently_and_preserves_order(client,monkeypatch):
    h=_login(client)
    client.post('/api/proxies',headers=h,json={'text':'134.209.144.56:80\n134.209.144.57:80\n134.209.144.58:80','enabled':False})
    live={'now':0,'peak':0}
    async def get(self,url,**kwargs):
        live['now']+=1;live['peak']=max(live['peak'],live['now'])
        try:
            await asyncio.sleep(0.05)
            return httpx.Response(200,json={'ip':'203.0.113.12'},request=httpx.Request('GET',url))
        finally:
            live['now']-=1
    monkeypatch.setattr(httpx.AsyncClient,'get',get)
    results=client.post('/api/proxies/batch-test',headers=h,json={'ids':[3,1,2]}).json()['results']
    assert [r['id'] for r in results]==[3,1,2]
    assert live['peak']>1 and all(r['ok'] for r in results)


def test_proxy_test_maps_socks_library_errors(client,monkeypatch):
    h=_login(client)
    client.post('/api/proxies',headers=h,json={'text':'socks5://45.74.31.47:10501','enabled':False})
    pytest.importorskip('python_socks')
    from python_socks._errors import ProxyConnectionError, ProxyTimeoutError
    async def timed_out(self,url,**kwargs):raise ProxyTimeoutError('Proxy connection timed out: 6.0')
    monkeypatch.setattr(httpx.AsyncClient,'get',timed_out)
    assert client.post('/api/proxies/1/test',headers=h).json()['error']=='连接超时'
    async def refused(self,url,**kwargs):raise ProxyConnectionError(None,"Couldn't connect to proxy")
    monkeypatch.setattr(httpx.AsyncClient,'get',refused)
    result=client.post('/api/proxies/1/test',headers=h).json()
    assert result['ok'] is False and result['error']=='无法连接到代理'


def test_proxy_test_survives_broken_socks_stream(client,monkeypatch):
    h=_login(client)
    client.post('/api/proxies',headers=h,json={'text':'socks5://45.74.31.47:10501\n134.209.144.56:80','enabled':False})
    anyio=pytest.importorskip('anyio')
    calls=[]
    async def dispatch(self,url,**kwargs):
        calls.append(1)
        if len(calls)==1:raise anyio.BrokenResourceError
        return httpx.Response(200,json={'ip':'203.0.113.20'},request=httpx.Request('GET',url))
    monkeypatch.setattr(httpx.AsyncClient,'get',dispatch)
    results=client.post('/api/proxies/batch-test',headers=h,json={'ids':[1,2]}).json()['results']
    assert len(results)==2
    assert results[0]['ok'] is False and results[0]['error']
    assert results[1]['ok'] is True


def test_proxy_test_explains_socks5h_is_not_probeable(client):
    h=_login(client)
    client.post('/api/proxies',headers=h,json={'text':'socks5h://proxy.example:1080','enabled':False})
    result=client.post('/api/proxies/1/test',headers=h).json()
    assert result['ok'] is False and 'socks5' in result['error'].lower()


def test_dynamic_proxy_is_flagged_and_samples_several_exit_ips(client,monkeypatch):
    h=_login(client)
    r=client.post('/api/proxies',headers=h,json={'text':'socks5://165.154.135.29:10000\nsocks5://165.154.135.29:10000','enabled':True,'is_dynamic':True})
    assert r.status_code==200,r.text
    body=r.json()
    assert body['total']==1 and body['items'][0]['is_dynamic'] is True
    calls=[]
    async def get(self,url,**kwargs):
        calls.append(1)
        return httpx.Response(200,json={'ip':f'203.0.113.{len(calls)}'},request=httpx.Request('GET',url))
    monkeypatch.setattr(httpx.AsyncClient,'get',get)
    result=client.post('/api/proxies/1/test',headers=h).json()
    assert len(calls)==3
    assert result['ok'] is True and result['exit_ips']==['203.0.113.1','203.0.113.2','203.0.113.3']
    assert result['exit_ip']=='203.0.113.1' and result['is_dynamic'] is True


def test_fixed_proxy_samples_once(client,monkeypatch):
    h=_login(client)
    client.post('/api/proxies',headers=h,json={'text':'134.209.144.56:80','enabled':False})
    calls=[]
    async def get(self,url,**kwargs):
        calls.append(1)
        return httpx.Response(200,json={'ip':'203.0.113.9'},request=httpx.Request('GET',url))
    monkeypatch.setattr(httpx.AsyncClient,'get',get)
    result=client.post('/api/proxies/1/test',headers=h).json()
    assert len(calls)==1 and result['exit_ips']==['203.0.113.9'] and result['is_dynamic'] is False


def test_dynamic_flag_can_be_toggled_and_survives_batch(client,monkeypatch):
    h=_login(client)
    client.post('/api/proxies',headers=h,json={'text':'socks5://165.154.135.29:10000','enabled':False})
    assert client.put('/api/proxies/1/dynamic',headers=h,json={'is_dynamic':True}).json()['items'][0]['is_dynamic'] is True
    assert client.get('/api/proxies',headers=h).json()['items'][0]['is_dynamic'] is True
    monkeypatch.setattr(httpx.AsyncClient,'get',fake_probe({'ip':'203.0.113.30'}))
    results=client.post('/api/proxies/batch-test',headers=h,json={'ids':[1]}).json()['results']
    assert results[0]['is_dynamic'] is True
    assert client.put('/api/proxies/1/dynamic',headers=h,json={'is_dynamic':False}).json()['items'][0]['is_dynamic'] is False


def test_dynamic_proxy_snapshot_reaches_ytc_as_plain_gateway(client):
    h=_login(client)
    client.post('/api/proxies',headers=h,json={'text':'socks5://165.154.135.29:10000','enabled':True,'is_dynamic':True})
    client.post('/api/credentials/import',headers=h,files={'file':('a.txt','a@example.com---pw---2FA---eyJoriginal')})
    client.put('/api/configs/upi_provider.cdk',headers=h,json={'value':'ohk_test'})
    r=client.post('/api/batches',headers=h,json={'name':'dyn','credential_ids':[1]})
    detail=client.get('/api/batches/'+str(r.json()['id']),headers=h).json()
    async def check():
        async with aiosqlite.connect(client._product_db_path) as db:
            raw=(await (await db.execute('SELECT snapshot_json FROM task_batches WHERE id=?',(detail['id'],))).fetchone())[0]
            assert json.loads(raw)['proxies']==['socks5://165.154.135.29:10000']
    asyncio.run(check())


def test_parse_proxy_accepts_host_port_user_pass():
    assert proxy_service.parse_proxy('us.proxy.test:10000:USER1-zone-custom-region-IN:pw123')=={
        'url':'http://USER1-zone-custom-region-IN:pw123@us.proxy.test:10000','protocol':'http',
        'host':'us.proxy.test','port':10000}
    assert proxy_service.parse_proxy('socks5://us.proxy.test:10000:USER1-region-IN:pw123')['url']==\
        'socks5://USER1-region-IN:pw123@us.proxy.test:10000'


def test_parse_proxy_still_rejects_ambiguous_and_short_forms():
    for bad in ('us.proxy.test:10000:onlyuser','us.proxy.test:10000:user:pw:extra','[::1]:8080:user:pw',''):
        with pytest.raises(ValueError):
            proxy_service.parse_proxy(bad)


def test_session_variant_appends_session_to_username():
    base='socks5://USER1-zone-custom-region-IN:pw@host.test:10000'
    assert proxy_service.session_variant(base,'12345678')=='socks5://USER1-zone-custom-region-IN-session-12345678:pw@host.test:10000'
    assert proxy_service.session_variant(proxy_service.session_variant(base,'12345678'),'99999999').count('session-')==1
    assert proxy_service.session_variant('socks5://host.test:10000','12345678')=='socks5://host.test:10000'


def _member_proxies(client,bid):
    async def read():
        async with aiosqlite.connect(client._product_db_path) as db:
            async with db.execute('SELECT credential_id,proxy_url FROM batch_members WHERE batch_id=? ORDER BY credential_id',(bid,)) as cur:
                return [tuple(r) for r in await cur.fetchall()]
    return asyncio.run(read())


def _prepare_sticky_batch(client,text,creds):
    h=_login(client)
    client.post('/api/proxies',headers=h,json={'text':text,'enabled':True,'is_dynamic':True})
    body='\n'.join(f'{name}@example.com---pw---2FA---eyJ{i}' for i,name in enumerate(creds))
    client.post('/api/credentials/import',headers=h,files={'file':('a.txt',body)})
    client.put('/api/configs/upi_provider.cdk',headers=h,json={'value':'ohk_test'})
    client.put('/api/configs/sms_provider.cdk',headers=h,json={'value':'sms_test'})
    return h


def test_task_assigns_a_distinct_sticky_session_per_account(client):
    h=_prepare_sticky_batch(client,'socks5://USER1-zone-custom-region-IN:pw123@us.proxy.test:10000',['a','b'])
    bid=client.post('/api/batches',headers=h,json={'name':'sticky','credential_ids':[1,2]}).json()['id']
    urls=[url for _,url in _member_proxies(client,bid)]
    assert all('session-' in url for url in urls),urls
    assert len(set(urls))==2,urls
    assert all(url.startswith('socks5://USER1-zone-custom-region-IN-session-') for url in urls)
    assert all(url.endswith(':pw123@us.proxy.test:10000') for url in urls)


def test_task_keeps_plain_proxy_when_gateway_has_no_credentials(client):
    h=_prepare_sticky_batch(client,'134.209.144.56:80',['a'])
    bid=client.post('/api/batches',headers=h,json={'name':'plain','credential_ids':[1]}).json()['id']
    assert _member_proxies(client,bid)[0][1]=='http://134.209.144.56:80'


def test_task_details_never_expose_proxy_credentials(client):
    h=_prepare_sticky_batch(client,'socks5://USER1-zone-custom-region-IN:pw123@us.proxy.test:10000',['a'])
    bid=client.post('/api/batches',headers=h,json={'name':'mask','credential_ids':[1]}).json()['id']
    detail=client.get(f'/api/batches/{bid}',headers=h).json()
    assert 'pw123' not in json.dumps(detail) and 'USER1' not in json.dumps(detail)
    assert all('proxy_url' not in m for m in detail['members'])


def test_retry_batch_keeps_the_assigned_sticky_session(client):
    h=_prepare_sticky_batch(client,'socks5://USER1-zone-custom-region-IN:pw123@us.proxy.test:10000',['a'])
    bid=client.post('/api/batches',headers=h,json={'name':'first','credential_ids':[1]}).json()['id']
    original=_member_proxies(client,bid)[0][1]
    async def mark():
        async with aiosqlite.connect(client._product_db_path) as db:
            await db.execute("UPDATE task_batches SET status='failed' WHERE id=?",(bid,))
            await db.execute("UPDATE batch_members SET stage='upi_failed' WHERE batch_id=?",(bid,));await db.commit()
    asyncio.run(mark())
    from app.services import batch_service as bs
    async def retry():
        svc=bs.BatchService(client._product_db_path)
        return await svc.create_retry_batch(bid)
    result=asyncio.run(retry())
    assert _member_proxies(client,result['batch_id'])[0][1]==original


def test_upi_step_uses_the_member_session_proxy(client,monkeypatch):
    h=_prepare_sticky_batch(client,'socks5://USER1-zone-custom-region-IN:pw123@us.proxy.test:10000',['a'])
    bid=client.post('/api/batches',headers=h,json={'name':'use','credential_ids':[1]}).json()['id']
    assigned=_member_proxies(client,bid)[0][1]
    assert 'session-' in assigned
    from app.services import batch_service as bs
    from app.services.qualification_service import CheckOutcome
    used=[]
    hit=asyncio.Event()
    class Qual:
        def __init__(self,db,**kwargs):pass
        async def check_single(self,at):return CheckOutcome(True,'eligible',{})
    class UPI:
        def __init__(self,db,**kwargs):self.ytc_proxies=kwargs.get('ytc_proxies',[])
        async def create_with_retries(self,*a,**k):
            used.extend(self.ytc_proxies);hit.set()
            return {'status':'failed','error_msg':'stop-after-capture'}
    monkeypatch.setattr(bs,'QualificationService',Qual)
    monkeypatch.setattr(bs,'UPIService',UPI)
    monkeypatch.setattr(bs.SMSService,'bind_with_retries',lambda self,cid:{'status':'failed','error_msg':'unused'})
    svc=bs.BatchService(client._product_db_path)
    async def drive():
        async with aiosqlite.connect(client._product_db_path) as db:
            await db.execute("UPDATE task_batches SET status='running' WHERE id=?",(bid,));await db.commit()
        task=asyncio.create_task(svc.run(bid))
        try:
            await asyncio.wait_for(hit.wait(),30)
        finally:
            await task
    asyncio.run(drive())
    assert used==[assigned]


def test_proxy_api_fetch_endpoint_is_gone(client):
    h=_login(client)
    assert client.post('/api/proxies/fetch-api',headers=h,json={'url':'https://proxy.example.test/gen'}).status_code==405
    page=Path(__file__).parents[1].joinpath('static/index.html').read_text()
    assert 'API 拉取' not in page and 'fetch-api' not in page


def test_proxy_batch_test_reports_missing_ids(client,monkeypatch):
    h=_login(client)
    client.post('/api/proxies',headers=h,json={'text':'134.209.144.56:80','enabled':False})
    monkeypatch.setattr(httpx.AsyncClient,'get',fake_probe({'ip':'203.0.113.13'}))
    results=client.post('/api/proxies/batch-test',headers=h,json={'ids':[1,999]}).json()['results']
    assert results[-1]=={'id':999,'ok':False,'error':'代理不存在'}


def fake_probe(payload):
    async def get(self,url,**kwargs):
        return httpx.Response(200,json=payload,request=httpx.Request('GET',url))
    return get


def scheme_recorder(used):
    original=proxy_service._client_for
    def wrapper(url,timeout):
        used.append(url.split('://',1)[0]);return original(url,timeout)
    return wrapper


def test_free_proxy_import_is_removed(client,monkeypatch):
    h=_login(client);called=[]
    async def get(self,url,**kwargs):
        called.append(str(url));return httpx.Response(200,json={},request=httpx.Request('GET',url))
    monkeypatch.setattr(httpx.AsyncClient,'get',get)
    assert client.post('/api/proxies/import-free',headers=h,json={'protocol':'socks5'}).status_code==405
    assert called==[]
    text=Path(__file__).parents[1].joinpath('static/index.html').read_text()
    assert '免费代理导入' not in text and 'import-free' not in text


def test_proxy_library_can_store_socks4_but_new_ytc_task_rejects_it(client):
    h=_login(client)
    r=client.post('/api/proxies',headers=h,json={'text':'socks4://10.0.0.1:1080','enabled':True})
    assert r.status_code==200
    client.post('/api/credentials/import',headers=h,files={'file':('a.txt','a@example.com---pw---2FA---eyJoriginal')})
    client.put('/api/configs/upi_provider.cdk',headers=h,json={'value':'ohk_test'})
    bid=client.post('/api/batches',headers=h,json={'name':'socks4','credential_ids':[1]}).json()['id']
    r=client.post(f'/api/batches/{bid}/start',headers=h)
    assert r.status_code==400 and 'SOCKS5' in r.json()['detail']


def test_legacy_upi_api_rejects_socks4_before_provider_request(client,monkeypatch):
    h=_login(client)
    client.post('/api/proxies',headers=h,json={'text':'socks4://10.0.0.1:1080','enabled':True})
    client.put('/api/configs/upi_provider.cdk',headers=h,json={'value':'ohk_test'})
    async def forbidden(*args,**kwargs):raise AssertionError('不应提交 SOCKS4 到 ytc')
    monkeypatch.setattr(httpx.AsyncClient,'post',forbidden)
    r=client.post('/api/upi/create',headers=h,json={'credential_ids':[1],'amount':100})
    assert r.status_code==400 and 'SOCKS5' in r.json()['detail']


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
    client.put('/api/configs/ytc.proxy_source',headers=h,json={'value':'provided'})
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
    assert r.json()['version']=='1.2.0'
    root=Path(__file__).parents[1]
    assert '1.2.0' in (root/'VERSION').read_text()


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
    client.put('/api/configs/ytc.proxy_source',headers=h,json={'value':'provided'})
    async def forbidden(*args,**kwargs):raise AssertionError('无代理时不能提交新单')
    monkeypatch.setattr(httpx.AsyncClient,'post',forbidden)
    r=client.post('/api/upi/create',headers=h,json={'credential_ids':[1],'amount':100,'retry_interval_s':0})
    assert r.status_code==400 and '代理' in r.json()['detail']


def test_manual_push_returns_each_selected_account_result_without_hiding_partial_success(client,monkeypatch):
    h=_login(client)
    client.post('/api/configs/batch',headers=h,json={'items':{'sub2api.enabled':'true','sub2api.base_url':'https://sub2.example','sub2api.admin_key':'admin-test'}})
    async def enqueue(db,cid,bid,snapshot):
        if cid==2:raise ValueError('未接码账号不能推送')
        return 11
    monkeypatch.setattr('app.services.integration_service.enqueue_push',enqueue)
    r=client.post('/api/integrations/sub2api/pushes',headers=h,json={'ids':[1,2]})
    assert r.status_code==200,r.text
    assert r.json()['count']==1 and r.json()['failed']==1
    assert r.json()['results']==[{'credential_id':1,'push_id':11,'status':'queued'},{'credential_id':2,'status':'rejected','error':'未接码账号不能推送'}]


def test_completed_sms_task_auto_enqueues_sub2api_and_manual_push_remains_available(client,monkeypatch):
    from app.services import batch_service as bs
    h=_login(client)
    client.post('/api/credentials/import',headers=h,files={'file':('a.txt','a@example.com---pw---2FA---eyJoriginal')})
    client.put('/api/configs/sms_provider.cdk',headers=h,json={'value':'sms-test'})
    client.put('/api/configs/sub2api.base_url',headers=h,json={'value':'https://sub2.example'})
    client.put('/api/configs/sub2api.admin_key',headers=h,json={'value':'admin-test'})
    client.put('/api/configs/sub2api.enabled',headers=h,json={'value':'true'})
    client.put('/api/proxies',headers=h,json={'text':'134.209.144.56:80','enabled':True})
    class Qual:
        def __init__(self,*args,**kwargs):pass
        async def check_single(self,at):return CheckOutcome(True,'eligible',{})
    class UPI:
        def __init__(self,db,**kwargs):self.db=db
        async def create_with_retries(self,cid,*args,**kwargs):
            await self.db.execute("INSERT INTO upi_tasks(credential_id,amount,status,upi_link) VALUES(?,1,'success','https://pay.example')",(cid,));await self.db.commit()
            return {'id':1,'status':'success'}
    class SMS:
        def __init__(self,db,**kwargs):self.db=db
        async def bind_with_retries(self,cid):
            await self.db.execute("INSERT INTO sms_tasks(credential_id,status,new_access_token) VALUES(1,'bound','eyJnew')")
            await self.db.commit()
            return {'id':1,'status':'bound'}
    enqueued=[]
    async def enqueue(*args,**kwargs):enqueued.append(args)
    monkeypatch.setattr(bs,'QualificationService',Qual);monkeypatch.setattr(bs,'UPIService',UPI);monkeypatch.setattr(bs,'SMSService',SMS)
    monkeypatch.setattr('app.services.integration_service.enqueue_push',enqueue)
    async def run():
        svc=bs.BatchService(client._product_db_path)
        bid=await svc.create_batch('手动推送',[1],100)
        async with aiosqlite.connect(client._product_db_path) as db:
            await db.execute("UPDATE task_batches SET status='running' WHERE id=?",(bid,));await db.commit()
        await svc.run(bid)
    asyncio.run(run())
    assert len(enqueued)==1


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
