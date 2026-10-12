"""ytc 的实际 tasks 结构可显示进度，长等待持续反馈而不暴露令牌/原始 JSON。"""
import httpx

from app.services.upi_providers import YtcUPIProvider


async def test_polling_emits_real_stage_percentage_and_payment_changes(monkeypatch):
    state={'summaries':0,'results':0}
    async def get(self,url,**kwargs):
        if '/api/tasks' in url:
            body={'items':[{'task':{'id':'t0001','stage':'stripe_init','progress':45,'current_round':1,
                                    'max_rounds':3,'elapsed_seconds':12,'access_token':'eyJDO_NOT_LOG',
                                    'error':None}}]}
        elif 'summary=true' in url:
            state['summaries']+=1
            body={'status':'running' if state['summaries']==1 else 'succeeded','counts':{'running':1}}
        else:
            state['results']+=1
            body={'items':[{'url':'https://pay.example','submission':{'status':'completed' if state['results']>1 else 'pending'}}]}
        return httpx.Response(200,json=body,request=httpx.Request('GET',url))
    monkeypatch.setattr(httpx.AsyncClient,'get',get)
    provider=YtcUPIProvider('https://ytc.example','ohk_DO_NOT_LOG',poll_interval=0,wait_for_payment=True)
    messages=[]
    async def progress(message):messages.append(message)
    provider.on_progress=progress
    async with httpx.AsyncClient() as client:
        polled=await provider.poll_results(client,'job-1')
        await provider._wait_payment(client,'job-1',polled['results'])
    assert any('初始化支付页' in m and '45%' in m for m in messages),messages
    assert any('第 1/3 轮' in m for m in messages)
    assert any('等待' in m and '支付' in m for m in messages)
    assert any('支付完成' in m for m in messages)
    assert not any('DO_NOT_LOG' in m or 'https://pay.example' in m for m in messages)


async def test_unchanged_progress_is_deduplicated_but_heartbeat_continues(monkeypatch):
    provider=YtcUPIProvider('https://ytc.example','ohk_secret')
    messages=[]
    async def progress(message):messages.append(message)
    provider.on_progress=progress
    now={'time':0.0}
    monkeypatch.setattr('app.services.upi_providers.time.monotonic',lambda:now['time'])
    await provider._emit_progress('等待支付 0 秒',('payment','pending'))
    now['time']=10
    await provider._emit_progress('等待支付 10 秒',('payment','pending'))
    assert len(messages)==1
    now['time']=30
    await provider._emit_progress('等待支付 30 秒',('payment','pending'))
    assert messages==['等待支付 0 秒','等待支付 30 秒']


async def test_progress_endpoint_failure_does_not_fail_original_order(monkeypatch):
    async def get(self,url,**kwargs):
        if '/api/tasks' in url:
            return httpx.Response(503,json={'detail':'busy'},request=httpx.Request('GET',url))
        body={'status':'succeeded'} if 'summary=true' in url else {'items':[{'url':'https://pay.example'}]}
        return httpx.Response(200,json=body,request=httpx.Request('GET',url))
    monkeypatch.setattr(httpx.AsyncClient,'get',get)
    provider=YtcUPIProvider('https://ytc.example','ohk_test',poll_interval=0)
    messages=[]
    async def progress(message):messages.append(message)
    provider.on_progress=progress
    async with httpx.AsyncClient() as client:
        result=await provider.poll_results(client,'job-1')
    assert result['status']=='succeeded'
    assert any('进度暂不可用' in m for m in messages)
