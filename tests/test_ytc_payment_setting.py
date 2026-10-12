"""ohk_ CDK 用于账户登录；未确认支付完成时不能进入接码。"""
import httpx

from app.services.upi_providers import YtcUPIProvider


async def test_cdk_logs_in_and_waits_for_payment_completion(monkeypatch):
    calls=[]
    result_reads=[]

    async def post(self,url,**kwargs):
        calls.append((url,kwargs.get('json')))
        body={'authenticated':True} if url.endswith('/api/auth/login') else {'id':'job-1'}
        return httpx.Response(200,json=body,request=httpx.Request('POST',url))

    async def get(self,url,**kwargs):
        if 'summary' in url:body={'status':'succeeded'}
        else:
            result_reads.append(url)
            body={'items':[{'url':'https://pay.example/1','submission':{'status':'completed' if len(result_reads)>1 else 'pending'}}]}
        return httpx.Response(200,json=body,request=httpx.Request('GET',url))

    monkeypatch.setattr(httpx.AsyncClient,'post',post)
    monkeypatch.setattr(httpx.AsyncClient,'get',get)
    provider=YtcUPIProvider('https://ytc.example','ohk_payment-cdk',poll_interval=0,wait_for_payment=True)
    link,raw=await provider.fetch_link('eyJoriginal')
    assert link=='https://pay.example/1'
    assert calls[0]==('https://ytc.example/api/auth/login',{'api_key':'ohk_payment-cdk'})
    assert len(result_reads)==2
    assert all('/api/console/settings' not in url for url,_ in calls)
