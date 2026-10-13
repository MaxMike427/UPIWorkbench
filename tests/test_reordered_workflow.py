"""最新规则：TXT 原 AT → ytc 提链支付 → 无限接码 → 成功库与任务 MD。"""
import asyncio
from pathlib import Path

import aiosqlite
import pytest

from app.database import init_db
from app.services import batch_service as bs
from app.services.credential_service import import_from_txt
from app.services.qualification_service import CheckOutcome


@pytest.fixture()
async def workflow(monkeypatch, tmp_path):
    db_path = str(tmp_path / "workflow.sqlite")
    monkeypatch.setenv("UPI_DB_PATH", db_path)
    monkeypatch.setenv("UPI_REPORT_DIR", str(tmp_path / "reports"))
    from app.config import get_settings
    get_settings.cache_clear()
    await init_db(db_path)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await import_from_txt(db, "a@example.com---secret-password---2FASECRET---eyJoriginal", "source.txt")
    yield db_path, tmp_path / "reports"
    get_settings.cache_clear()


async def test_payment_uses_imported_at_before_sms_and_completed_task_writes_safe_md(workflow, monkeypatch):
    db_path, report_dir = workflow
    calls = []

    class Qual:
        def __init__(self, db, **kwargs): pass
        async def check_single(self, at):
            calls.append(("qualification",at))
            return CheckOutcome(True,"eligible",{"token_ok":True,"eligible":True})

    class UPI:
        def __init__(self, db, **kwargs): self.db=db
        async def create_with_retries(self, cid, amount, note="", callback_url="", at_token=None):
            calls.append(("upi", at_token))
            await self.db.execute("INSERT INTO upi_tasks(credential_id,amount,status,upi_link) VALUES(?,100,'success','https://pay.example')", (cid,))
            await self.db.commit()
            return {"id":1,"status":"success"}

    class SMS:
        def __init__(self, db, **kwargs):
            self.db=db
            assert kwargs["max_retries"] is None
        async def bind_with_retries(self,cid):
            calls.append(("sms", cid))
            await self.db.execute("INSERT INTO sms_tasks(credential_id,status,new_access_token) VALUES(?,'bound','eyJnew')", (cid,))
            await self.db.commit()
            return {"id":1,"status":"bound"}

    monkeypatch.setattr(bs,"UPIService",UPI)
    monkeypatch.setattr(bs,"SMSService",SMS)
    monkeypatch.setattr(bs,"QualificationService",Qual)
    svc=bs.BatchService(db_path, sms={"cdk":"PRIVATE-CDK-123","max_retries":None}, upi={})
    bid=await svc.create_batch("任务",[1],amount=100)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE credentials SET access_token='eyJchanged' WHERE id=1")
        await db.execute("UPDATE task_batches SET status='running' WHERE id=?",(bid,))
        await db.commit()
    await svc.run(bid)
    assert calls == [("qualification","eyJoriginal"),("upi","eyJoriginal"),("sms",1)]
    detail=await svc.get_progress(bid)
    assert detail["status"]=="completed"
    assert detail["members"][0]["stage"]=="done"
    reports=list(report_dir.rglob("*.md"))
    assert len(reports)==1
    text=reports[0].read_text()
    assert "a@example.com" in text and "成功" in text
    for secret in ("secret-password","2FASECRET","eyJoriginal","eyJnew","PRIVATE-CDK-123"):
        assert secret not in text


async def test_upi_failure_never_submits_sms_and_writes_failure_report(workflow, monkeypatch):
    db_path, report_dir=workflow
    class UPI:
        def __init__(self, db, **kwargs): pass
        async def create_with_retries(self,*args,**kwargs):return {"status":"failed","error_msg":"no_link"}
    class SMS:
        def __init__(self,*args,**kwargs):raise AssertionError("未完成提链支付不能接码")
    class Qual:
        def __init__(self,db,**kwargs): pass
        async def check_single(self,at):return CheckOutcome(True,"eligible",{})
    monkeypatch.setattr(bs,"UPIService",UPI)
    monkeypatch.setattr(bs,"SMSService",SMS)
    monkeypatch.setattr(bs,"QualificationService",Qual)
    svc=bs.BatchService(db_path)
    bid=await svc.create_batch("失败任务",[1],100)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE task_batches SET status='running' WHERE id=?",(bid,));await db.commit()
    await svc.run(bid)
    detail=await svc.get_progress(bid)
    assert detail["status"]=="failed"
    assert detail["members"][0]["stage"]=="upi_failed"
    assert len(list(report_dir.rglob("*.md")))==1


async def test_ineligible_account_never_enters_payment_or_sms(workflow,monkeypatch):
    db_path,report_dir=workflow
    class Qual:
        def __init__(self,db,**kwargs):pass
        async def check_single(self,at):return CheckOutcome(False,"not-eligible",{})
    class Forbidden:
        def __init__(self,*args,**kwargs):raise AssertionError("无资格不得提链或接码")
    monkeypatch.setattr(bs,"QualificationService",Qual)
    monkeypatch.setattr(bs,"UPIService",Forbidden)
    monkeypatch.setattr(bs,"SMSService",Forbidden)
    svc=bs.BatchService(db_path)
    bid=await svc.create_batch("无资格",[1],100)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE task_batches SET status='running' WHERE id=?",(bid,));await db.commit()
    await svc.run(bid)
    detail=await svc.get_progress(bid)
    assert detail["members"][0]["stage"]=="qual_failed"
    assert detail["status"]=="failed"
    assert len(list(report_dir.rglob("*.md")))==1


async def test_sms_retries_beyond_five_until_success_and_can_pause(workflow, monkeypatch):
    from app.services.sms_service import SMSService
    db_path,_=workflow
    async with aiosqlite.connect(db_path) as db:
        db.row_factory=aiosqlite.Row
        svc=SMSService(db,"https://handy.example","cdk",max_retries=None,retry_interval=0)
        attempts=[]
        async def attempt(cid,no):
            attempts.append(no)
            return {"status":"bound" if no==6 else "failed"}
        monkeypatch.setattr(svc,"_attempt",attempt)
        result=await svc.bind_with_retries(1)
        assert result["status"]=="bound"
        assert attempts==list(range(7))
        async def paused():return False
        svc.should_continue=paused
        attempts.clear()
        result=await svc.bind_with_retries(1)
        assert result["status"]=="paused" and attempts==[]


async def test_payment_pause_resumes_original_job_instead_of_resubmitting(workflow,monkeypatch):
    import httpx
    from app.services.upi_service import UPIService
    db_path,_=workflow
    submissions=[]
    running={'value':False}
    async def post(self,url,**kwargs):
        if url.endswith('/api/auth/login'):body={'authenticated':True}
        else:submissions.append(url);body={'id':'external-original'}
        return httpx.Response(200,json=body,request=httpx.Request('POST',url))
    async def get(self,url,**kwargs):
        body={'status':'succeeded'} if 'summary' in url else {'items':[{'url':'https://pay.example/1','submission':{'status':'completed' if running['value'] else 'pending'}}]}
        return httpx.Response(200,json=body,request=httpx.Request('GET',url))
    monkeypatch.setattr(httpx.AsyncClient,'post',post);monkeypatch.setattr(httpx.AsyncClient,'get',get)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory=aiosqlite.Row
        svc=UPIService(db,ytc_api_key='ohk_test',wait_for_payment=True,ytc_poll_interval=0,retry_interval=0)
        async def alive():return running['value']
        svc.provider.should_continue=alive
        first=await svc.create_with_retries(1,100,at_token='eyJoriginal')
        assert first['status']=='paused'
        running['value']=True
        second=await svc.create_with_retries(1,100,at_token='eyJoriginal')
        assert second['status']=='success'
        assert len(submissions)==1


async def test_post_payment_sms_export_failure_does_not_rebind_same_account(workflow,monkeypatch):
    from app.services.sms_service import SMSService
    db_path,_=workflow
    async with aiosqlite.connect(db_path) as db:
        db.row_factory=aiosqlite.Row
        svc=SMSService(db,'https://handy.example','cdk',max_retries=None,poll_interval=0,retry_interval=0)
        submits=[];exports=[]
        async def submit(*args):submits.append(1);return 'sms-original'
        async def live(oid):return {'id':oid,'status':'ok'}
        async def export(oid,fmt=None):
            exports.append(1)
            if len(exports)==1:raise ValueError('temporary export unavailable')
            from tests.test_sms import ZIP_BYTES
            return ZIP_BYTES
        monkeypatch.setattr(svc,'_submit',submit);monkeypatch.setattr(svc,'_live',live);monkeypatch.setattr(svc,'_export_zip',export)
        monkeypatch.setattr('app.services.sms_service.extract_new_at',lambda *args,**kwargs:'eyJnew')
        result=await svc.bind_with_retries(1)
        assert result['status']=='bound'
        assert len(submits)==1 and len(exports)==3


async def test_upi_default_allows_five_attempts_after_qualification(workflow,monkeypatch):
    from app.services.upi_service import UPIService
    db_path,_=workflow
    async with aiosqlite.connect(db_path) as db:
        db.row_factory=aiosqlite.Row
        svc=UPIService(db,ytc_api_key='ohk_test',retry_interval=0)
        attempts=[]
        async def attempt(*args,**kwargs):
            attempts.append(kwargs.get('at_token'))
            return {'status':'success' if len(attempts)==5 else 'failed','error_msg':'no_link'}
        monkeypatch.setattr(svc,'create_upi_link',attempt)
        result=await svc.create_with_retries(1,100,at_token='eyJoriginal')
        assert result['status']=='success'
        assert attempts==['eyJoriginal']*5


async def test_waiting_payment_interruption_does_not_resubmit(workflow,monkeypatch):
    import httpx
    from app.services.upi_service import UPIService
    from app.services.upi_providers import ExecutionPaused
    db_path,_=workflow
    async with aiosqlite.connect(db_path) as db:
        db.row_factory=aiosqlite.Row
        svc=UPIService(db,ytc_api_key='ohk_test',wait_for_payment=True,retry_interval=0)
        calls=[]
        async def fetch(at,concurrency,job_id=None):
            calls.append(job_id)
            await svc.provider.on_submitted('existing-job')
            raise httpx.ReadTimeout('network disconnected after submit')
        monkeypatch.setattr(svc.provider,'fetch_link',fetch)
        result=await svc.create_with_retries(1,100,at_token='eyJoriginal')
        assert result['status']=='paused'
        assert len(calls)==1


async def test_ytc_needs_review_pauses_original_order_without_new_payment(workflow,monkeypatch):
    import httpx
    from app.services.upi_service import UPIService
    db_path,_=workflow
    calls=[]
    async def post(self,url,**kwargs):
        if url.endswith('/api/auth/login'):body={'ok':True}
        else:calls.append(url);body={'id':'original-needs-review'}
        return httpx.Response(200,json=body,request=httpx.Request('POST',url))
    async def get(self,url,**kwargs):
        body={'items':[]} if '/api/tasks' in url else {'status':'needs_review','settings':{'proxies':'socks5://u:secret@proxy.example:1080'}}
        return httpx.Response(200,json=body,request=httpx.Request('GET',url))
    monkeypatch.setattr(httpx.AsyncClient,'post',post);monkeypatch.setattr(httpx.AsyncClient,'get',get)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory=aiosqlite.Row
        svc=UPIService(db,ytc_api_key='ohk_test',wait_for_payment=True,retry_interval=0,ytc_poll_interval=0)
        result=await svc.create_with_retries(1,100)
        assert result['status']=='paused' and 'secret' not in result['error_msg']
        result=await svc.create_with_retries(1,100)
        assert result['status']=='paused' and len(calls)==1


async def test_new_success_library_requires_sms_after_payment(workflow):
    from app.services.success_service import count_successes
    db_path,_=workflow
    async with aiosqlite.connect(db_path) as db:
        db.row_factory=aiosqlite.Row
        await db.execute("INSERT INTO task_batches(name,status,workflow_version) VALUES('partial','running',2)")
        await db.execute("INSERT INTO batch_members(batch_id,credential_id,stage) VALUES(1,1,'paid')")
        await db.execute("INSERT INTO upi_tasks(credential_id,amount,status,upi_link) VALUES(1,100,'success','https://pay.example')")
        await db.commit()
        assert await count_successes(db)==0
        await db.execute("UPDATE batch_members SET stage='done' WHERE batch_id=1")
        await db.commit()
        assert await count_successes(db)==0
        await db.execute("INSERT INTO sms_tasks(credential_id,status,new_access_token) VALUES(1,'bound','eyJnew')")
        await db.execute("UPDATE batch_members SET upi_task_id=1,sms_task_id=1 WHERE batch_id=1")
        await db.commit()
        assert await count_successes(db)==1


async def test_upi_retry_stops_if_task_pauses_during_interval(workflow,monkeypatch):
    from app.services.upi_service import UPIService
    db_path,_=workflow
    async with aiosqlite.connect(db_path) as db:
        db.row_factory=aiosqlite.Row
        svc=UPIService(db,ytc_api_key='ohk_test',retry_interval=0)
        calls=[]
        async def alive():return len(calls)==0
        async def attempt(*args,**kwargs):calls.append(1);return {'status':'failed','error_msg':'no_link'}
        svc.provider.should_continue=alive
        monkeypatch.setattr(svc,'create_upi_link',attempt)
        result=await svc.create_with_retries(1,100)
        assert result['status']=='paused' and len(calls)==1


async def test_failed_account_updates_library_before_other_payment_finishes(workflow,monkeypatch):
    db_path,_=workflow
    async with aiosqlite.connect(db_path) as db:
        db.row_factory=aiosqlite.Row
        await import_from_txt(db,'b@example.com---pw---2FA---eyJb','test.txt')
    entered=asyncio.Event()
    failed=asyncio.Event()
    finish=asyncio.Event()
    class Qual:
        def __init__(self,db,**kwargs):pass
        async def check_single(self,at):return CheckOutcome(True,'eligible',{})
    class UPI:
        def __init__(self,db,**kwargs):pass
        async def create_with_retries(self,cid,*args,**kwargs):
            if cid==1:
                await entered.wait()
                failed.set()
                return {'status':'failed','error_msg':'no_link'}
            entered.set()
            await finish.wait()
            return {'status':'failed','error_msg':'no_link'}
    monkeypatch.setattr(bs,'QualificationService',Qual)
    monkeypatch.setattr(bs,'UPIService',UPI)
    svc=bs.BatchService(db_path)
    bid=await svc.create_batch('并行账号',[1,2],100)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE task_batches SET status='running' WHERE id=?",(bid,));await db.commit()
    task=asyncio.create_task(svc.run(bid))
    try:
        await asyncio.wait_for(failed.wait(),5)
        for _ in range(100):
            async with aiosqlite.connect(db_path) as db:
                stage=(await (await db.execute('SELECT stage FROM batch_members WHERE batch_id=? AND credential_id=1',(bid,))).fetchone())[0]
            if stage=='upi_failed':break
            await asyncio.sleep(.01)
        async with aiosqlite.connect(db_path) as db:
            statuses=await (await db.execute('SELECT status,error_msg FROM credentials ORDER BY id')).fetchall()
        assert statuses[0]==('failed','no_link')
        assert statuses[1][0]=='processing'
        assert not task.done()
    finally:
        finish.set()
        await asyncio.wait_for(task,5)


async def test_startup_sync_repairs_failed_accounts_even_with_new_unstarted_task(workflow):
    db_path,_=workflow
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT INTO task_batches(name,status) VALUES('旧失败','failed')")
        await db.execute("INSERT INTO batch_members(batch_id,credential_id,stage,error) VALUES(1,1,'upi_failed','no_link')")
        await db.execute("INSERT INTO task_batches(name,status) VALUES('未启动','created')")
        await db.execute("INSERT INTO batch_members(batch_id,credential_id,stage) VALUES(2,1,'pending')")
        await db.commit()
    await bs.synchronize_account_statuses(db_path)
    async with aiosqlite.connect(db_path) as db:
        row=await (await db.execute('SELECT status,error_msg FROM credentials WHERE id=1')).fetchone()
    assert row==('failed','no_link')


async def test_configured_concurrency_really_limits_parallel_account_execution(workflow,monkeypatch):
    db_path,_=workflow
    async with aiosqlite.connect(db_path) as db:
        db.row_factory=aiosqlite.Row
        await import_from_txt(db,'b@example.com---pw---2FA---eyJb\nc@example.com---pw---2FA---eyJc','test.txt')
    active=0
    peak=0
    class Qual:
        def __init__(self,db,**kwargs):pass
        async def check_single(self,at):
            nonlocal active,peak
            active+=1;peak=max(peak,active)
            await asyncio.sleep(.02)
            active-=1
            return CheckOutcome(False,'not-eligible',{})
    monkeypatch.setattr(bs,'QualificationService',Qual)
    svc=bs.BatchService(db_path,concurrency=1)
    bid=await svc.create_batch('串行',[1,2,3],100)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE task_batches SET status='running' WHERE id=?",(bid,));await db.commit()
    await svc.run(bid)
    assert peak==1


async def test_upi_retries_report_attempt_and_failure_before_next_attempt(workflow,monkeypatch):
    from app.services.upi_service import UPIService
    db_path,_=workflow
    async with aiosqlite.connect(db_path) as db:
        db.row_factory=aiosqlite.Row
        svc=UPIService(db,ytc_api_key='ohk_test',max_retries=2,retry_interval=0)
        calls=[];messages=[]
        async def attempt(*args,**kwargs):
            calls.append(1)
            return {'status':'failed','error_msg':'no_link'} if len(calls)==1 else {'status':'success'}
        async def progress(message):messages.append(message)
        svc.provider.on_progress=progress
        monkeypatch.setattr(svc,'create_upi_link',attempt)
        await svc.create_with_retries(1,100)
        assert any('第 1/2 次' in m for m in messages)
        assert any('第 2/2 次' in m for m in messages)
        assert any('未获取到支付链接' in m and '重试' in m for m in messages)
