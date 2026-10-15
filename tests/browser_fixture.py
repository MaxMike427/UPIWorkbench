"""浏览器验收专用本地服务：真实 API/SQLite，第三方操作全为内存测试替身。"""
import os
import io
import json
import zipfile

import httpx
import uvicorn

from app.services import batch_service, proxy_service
from app.services.qualification_service import CheckOutcome
from app.services.sms_service import SMSService
from app.services.upi_service import UPIService
from app.services.account_export_service import save_account_exports

REMOTE_ACCOUNTS=[]
PROBE_COUNT=[0]


async def integration_get(self,url,**kwargs):
    if str(url)=='https://example.com/healthz':
        return httpx.Response(204,request=httpx.Request('GET',url))
    if not str(url).startswith('https://sub2api.example.test/api/v1/admin/'):
        raise AssertionError('浏览器验收禁止访问真实第三方接口')
    assert kwargs['headers']['x-api-key']=='admin-browser-test'
    if url.endswith('/groups'):
        data={'items':[{'id':7,'name':'Test OpenAI','platform':'openai','status':'active'}],'total':1}
    elif url.endswith('/accounts'):
        data={'items':REMOTE_ACCOUNTS,'total':len(REMOTE_ACCOUNTS)}
    else:raise AssertionError(url)
    return httpx.Response(200,json={'code':0,'data':data},request=httpx.Request('GET',url))


async def probe_get_result(url):
    assert proxy_service.PROBE_TARGET=='https://api.ipify.org?format=json'
    assert proxy_service.PROBE_TIMEOUT==6.0
    PROBE_COUNT[0]+=1
    return {'ok':True,'latency_ms':800+PROBE_COUNT[0]*20,'exit_ip':f'203.0.113.{PROBE_COUNT[0]}'}


async def integration_post(self,url,**kwargs):
    assert url=='https://sub2api.example.test/api/v1/admin/accounts/import/codex-session'
    assert kwargs['headers']['x-api-key']=='admin-browser-test'
    body=kwargs['json']
    assert body['group_ids']==[7] and body['concurrency']==4 and body['rate_multiplier']==1.5
    assert body['credential_extras']=={'model_mapping':{'gpt-5':'gpt-5'}}
    cid=501+len(REMOTE_ACCOUNTS)
    REMOTE_ACCOUNTS.append({'id':cid,'credentials':json.loads(body['content'])})
    return httpx.Response(200,json={'code':0,'data':{'created':1,'failed':0,'items':[{'index':1,'action':'created','account_id':cid}]}},request=httpx.Request('POST',url))


httpx.AsyncClient.get=integration_get
httpx.AsyncClient.post=integration_post
proxy_service._probe=probe_get_result


async def dynamic_fetch(config):
    assert config['api_url']=='https://api.711proxy.com/gen?token=browser-private'
    return [proxy_service.parse_proxy('http://203.0.113.71:10000')]


proxy_service.fetch_dynamic_proxies=dynamic_fetch


class Qual:
    def __init__(self,db,**kwargs):pass
    async def check_single(self,at):return CheckOutcome(True,'eligible',{'token_ok':True,'eligible':True})


class UPI(UPIService):
    async def create_with_retries(self,cid,amount,note='',callback_url='',at_token=None):
        async with self.db.execute("INSERT INTO upi_tasks(credential_id,amount,status,upi_link) VALUES(?,?,'success','https://pay.example/test')",(cid,amount)) as cur:
            task_id=cur.lastrowid
        await self.db.commit()
        return {'id':task_id,'status':'success'}


class SMS(SMSService):
    async def bind_with_retries(self,cid):
        async with self.db.execute("INSERT INTO sms_tasks(credential_id,status,new_access_token) VALUES(?,'bound','eyJbrowser-test')",(cid,)) as cur:
            task_id=cur.lastrowid
        await self.db.commit()
        archives={}
        for fmt in ('cpa','sub2'):
            buf=io.BytesIO()
            with zipfile.ZipFile(buf,'w') as z:z.writestr(fmt+'.json',json.dumps({'access_token':f'eyJbrowser-test-{cid}','refresh_token':f'rt.browser-test-{cid}','format':fmt}))
            archives[fmt]=buf.getvalue()
        await save_account_exports(self.db,cid,task_id,archives)
        return {'id':task_id,'status':'bound'}


batch_service.QualificationService=Qual
batch_service.UPIService=UPI
batch_service.SMSService=SMS
from app.main import app

if __name__=='__main__':
    uvicorn.run(app,host='127.0.0.1',port=int(os.environ.get('UPI_TEST_PORT','8891')))
