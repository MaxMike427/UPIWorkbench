"""任务编排：原 AT 资格检查 → ytc 提链支付 → 无限接码 → 全流程成功。"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime

import aiosqlite

from ..database import log_runtime
from ..config import parse_concurrency
from ..utils.helpers import utcnow_iso
from .qualification_service import QualificationService
from .sms_service import SMSService
from .upi_service import UPIService
from .report_service import safe_error, write_task_report

_RUNNING: dict[tuple[str,int], asyncio.Task] = {}

ACCOUNT_STAGE_STATUS = {
    'pending':'pending', 'qualified':'qualified', 'paid':'processing', 'binding':'processing',
    'done':'done', 'upi_ok':'done', 'bound':'processing',
    'qual_failed':'failed', 'upi_failed':'failed', 'bind_failed':'failed',
    'paid_without_sms':'paid_without_sms',
}


async def synchronize_account_statuses(db_path: str) -> None:
    """修复升级前只写成员阶段未写账号状态的旧记录，以最新未删除任务为准。"""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory=aiosqlite.Row
        async with db.execute(
            "SELECT m.credential_id,m.stage,m.error FROM batch_members m JOIN task_batches b ON b.id=m.batch_id"
            " WHERE b.deleted_at IS NULL AND m.batch_id=(SELECT MAX(bm.batch_id) FROM batch_members bm"
            " JOIN task_batches tb ON tb.id=bm.batch_id WHERE bm.credential_id=m.credential_id AND tb.deleted_at IS NULL AND bm.stage!='pending')"
            " AND m.stage!='pending'"
        ) as cur:
            rows=[dict(row) async for row in cur]
        for row in rows:
            status=ACCOUNT_STAGE_STATUS.get(row['stage'])
            if status:
                await db.execute('UPDATE credentials SET status=?,error_msg=? WHERE id=?',
                                 (status,row['error'] or '',row['credential_id']))
        await db.commit()


async def reset_stuck_batches(db_path: str) -> int:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("UPDATE task_batches SET status='paused' WHERE status='running'") as cur:
            n=cur.rowcount
        await db.commit()
        return n


def _counts(stages:list[str]) -> dict:
    c={"total":len(stages),"qualified":0,"failed_qual":0,"paid":0,"upi_ok":0,"failed_upi":0,
       "bound":0,"success":0,"paid_without_sms":0,"failed_bind":0,"pending":0}
    for s in stages:
        if s in ('qualified','upi_failed','paid','binding','bind_failed','done','bound','upi_ok','paid_without_sms'):
            c['qualified']+=1
        if s in ('paid','binding','bind_failed','done','upi_ok','paid_without_sms'):
            c['paid']+=1
        if s in ('done','upi_ok'):
            c['success']+=1;c['bound']+=1;c['upi_ok']+=1
        if s=='qual_failed':c['failed_qual']+=1
        if s=='upi_failed':c['failed_upi']+=1
        if s=='bind_failed':c['failed_bind']+=1
        if s=='pending':c['pending']+=1
        if s=='paid_without_sms':c['paid_without_sms']+=1
    return c


class BatchService:
    def __init__(self, db_path:str, qual:dict|None=None,sms:dict|None=None,upi:dict|None=None,concurrency:int=3):
        self.db_path=db_path
        self.qual=qual or {}
        self.sms=sms or {}
        self.upi=upi or {}
        self.concurrency=concurrency

    @classmethod
    async def from_db(cls,db,db_path:str):
        async def cfg(key,default=''):
            async with db.execute('SELECT value FROM configs WHERE key=?',(key,)) as cur:row=await cur.fetchone()
            return row['value'] if row else default
        concurrency=parse_concurrency(await cfg('app.concurrency','3'))
        qual={'api_url':await cfg('qualification.api_url','https://cha.nerver.cc'),
              'api_key':await cfg('qualification.api_key'),'promo_id':await cfg('qualification.promo_id','plus-1-month-free')}
        sms={'base_url':await cfg('sms_provider.base_url','https://apihub.space/handy'),'cdk':await cfg('sms_provider.cdk'),
              'concurrency':concurrency,'max_retries':None,'retry_interval':120.0}
        upi={'provider':'ytc','ytc_base_url':await cfg('upi_provider.base_url','https://ytc.xxsyun.xyz'),
             'ytc_api_key':await cfg('upi_provider.cdk') or await cfg('upi_provider.api_key'),
             'wait_for_payment':True,'max_retries':5,'retry_interval':120.0,'concurrency':concurrency}
        return cls(db_path,qual,sms,upi,concurrency=concurrency)

    async def _connect(self):
        db=await aiosqlite.connect(self.db_path)
        db.row_factory=aiosqlite.Row
        return db

    async def create_batch(self,name:str,credential_ids:list[int],amount:int=10000,note:str='')->int:
        db=await self._connect()
        try:
            ids=list(dict.fromkeys(credential_ids))
            if not name.strip() or not ids:raise ValueError('请输入任务名称并选择账号')
            async with db.execute(f"SELECT id,COALESCE(original_access_token,access_token,'') AS at FROM credentials"
                                  f" WHERE id IN ({','.join('?' for _ in ids)})",ids) as cur:
                tokens={r['id']:r['at'] async for r in cur}
            if len(tokens)!=len(ids):raise ValueError('部分选中账号不存在，请刷新账号库重新选择')
            snapshot=await self._snapshot(db)
            async with db.execute("INSERT INTO task_batches(name,total_credentials,status,current_step,progress_json,amount,note,workflow_version,snapshot_json)"
                                  " VALUES(?,?,'created','created',?,?,?,3,?)",
                                  (name.strip(),len(ids),json.dumps(_counts(['pending']*len(ids))),amount,note,json.dumps(snapshot))) as cur:
                bid=cur.lastrowid
            for cid in ids:
                await db.execute("INSERT INTO batch_members(batch_id,credential_id,at_snapshot) VALUES(?,?,?)",(bid,cid,tokens[cid]))
            await db.commit()
            return bid
        finally:await db.close()

    async def _snapshot(self,db):
        async with db.execute('SELECT key,value FROM configs') as cur:config={r['key']:r['value'] async for r in cur}
        async with db.execute('SELECT url FROM proxies ORDER BY id') as cur:proxies=[r['url'] async for r in cur]
        return {'sms_enabled':config.get('sms.enabled','true')=='true','proxy_enabled':config.get('proxy.enabled','false')=='true',
                'proxies':proxies,'integration_enabled':config.get('sub2api.enabled','false')=='true',
                'integration_url':config.get('sub2api.base_url',''),'integration_template':json.loads(config.get('sub2api.template') or '{}')}

    @staticmethod
    def _legacy_snapshot():
        """V1.0 任务没有快照，重试必须沿用原流程而非读取当前全局开关。"""
        return {'legacy':True,'sms_enabled':True,'proxy_enabled':False,'proxies':[],
                'integration_enabled':False,'integration_url':'',
                'integration_template':{}}

    async def validate_start(self,db,bid):
        async with db.execute('SELECT * FROM task_batches WHERE id=? AND deleted_at IS NULL',(bid,)) as cur:batch=await cur.fetchone()
        if not batch:raise ValueError('任务不存在')
        snapshot=json.loads(batch['snapshot_json'] or '{}')
        if any(str(proxy).split('://',1)[0].lower() not in ('http','https','socks5') for proxy in snapshot.get('proxies',[])):
            raise ValueError('任务代理包含 ytc 不支持的协议，仅支持 HTTP/HTTPS/SOCKS5')
        if snapshot.get('sms_enabled',True) and not self.sms.get('cdk'):raise ValueError('请先保存接码 CDK，或为新任务关闭接码')
        async with db.execute("SELECT 1 FROM batch_members WHERE batch_id=? AND stage IN ('pending','qualified','upi_failed','qual_failed') LIMIT 1",(bid,)) as cur:requires_payment=await cur.fetchone()
        if requires_payment and not self.upi.get('ytc_api_key'):raise ValueError('请先保存 ytc 提链支付 CDK')
        async with db.execute("SELECT m.credential_id FROM batch_members m WHERE m.batch_id=? AND m.stage IN ('pending','qualified','upi_failed','qual_failed')"
                              " AND NOT EXISTS(SELECT 1 FROM upi_tasks u WHERE u.credential_id=m.credential_id AND u.status='processing' AND u.external_job_id IS NOT NULL)",(bid,)) as cur:
            requires_proxy=await cur.fetchone()
        if batch['workflow_version'] >= 3 and not snapshot.get('legacy') and requires_proxy and (not snapshot.get('proxy_enabled') or not snapshot.get('proxies')):
            raise ValueError('新提链需要开启代理库并保存有效代理，请重新创建含代理快照的任务')

    def start(self,batch_id:int):
        key=(self.db_path,batch_id)
        if key in _RUNNING and not _RUNNING[key].done():raise RuntimeError('任务当前步骤仍在执行，请稍后再试')
        _RUNNING[key]=asyncio.create_task(self.run(batch_id))

    async def _set_status(self,db,bid,status,step=None):
        if step is None:await db.execute('UPDATE task_batches SET status=? WHERE id=?',(status,bid))
        else:await db.execute('UPDATE task_batches SET status=?,current_step=? WHERE id=?',(status,step,bid))
        await db.commit()

    async def _alive(self,db,bid):
        async with db.execute('SELECT status FROM task_batches WHERE id=?',(bid,)) as cur:row=await cur.fetchone()
        return bool(row) and row['status']=='running'

    async def _members(self,db,bid,stages=None):
        sql=("SELECT m.credential_id,m.stage,m.error,m.updated_at,m.at_snapshot,m.upi_task_id,m.sms_task_id,c.account"
             " FROM batch_members m LEFT JOIN credentials c ON c.id=m.credential_id WHERE m.batch_id=?")
        args=[bid]
        if stages:
            sql+=f" AND m.stage IN ({','.join('?' for _ in stages)})";args+=sorted(stages)
        async with db.execute(sql+' ORDER BY m.credential_id',args) as cur:return [dict(r) async for r in cur]

    async def _save_progress(self,db,bid):
        members=await self._members(db,bid)
        await db.execute('UPDATE task_batches SET progress_json=? WHERE id=?',(json.dumps(_counts([m['stage'] for m in members])),bid))
        await db.commit()

    async def _set_stage(self,db,bid,cid,stage,error='',upi_task_id=None,sms_task_id=None):
        await db.execute("UPDATE batch_members SET stage=?,error=?,updated_at=?,upi_task_id=COALESCE(?,upi_task_id),"
                         " sms_task_id=COALESCE(?,sms_task_id) WHERE batch_id=? AND credential_id=?",
                         (stage,error[:500],utcnow_iso(),upi_task_id,sms_task_id,bid,cid))
        await db.execute('UPDATE credentials SET status=?,error_msg=?,updated_at=? WHERE id=?',
                         (ACCOUNT_STAGE_STATUS.get(stage,'processing'),error[:500],utcnow_iso(),cid))
        await db.commit()
        await self._save_progress(db,bid)

    async def run(self,batch_id:int):
        db=await self._connect()
        try:
            if not await self._alive(db,batch_id):return
            async with db.execute('SELECT * FROM task_batches WHERE id=?',(batch_id,)) as cur:batch=await cur.fetchone()
            snapshot=json.loads(batch['snapshot_json'] or '{}')
            # 新建任务统一采用最新版流程；历史成功记录原样保留。
            for step,stages in [('qualification',{'pending'}),('upi',{'qualified'}),('sms',{'paid','binding'})]:
                if not await self._alive(db,batch_id):return
                await db.execute('UPDATE task_batches SET current_step=? WHERE id=? AND status=\'running\'',(step,batch_id))
                await db.commit()
                members=await self._members(db,batch_id,stages)
                if not members:continue
                if step=='sms' and not snapshot.get('sms_enabled',True):
                    for member in members:
                        await self._set_stage(db,batch_id,member['credential_id'],'paid_without_sms')
                    await log_runtime(db,f'任务 #{batch_id}：接码关闭，支付成功账号标记为已成功未接码',batch_id=batch_id)
                    continue
                await log_runtime(db,f"任务 #{batch_id}：开始{'资格检查' if step=='qualification' else '提链支付' if step=='upi' else '无限重试接码'}，共 {len(members)} 个账号",batch_id=batch_id)
                sem=asyncio.Semaphore(self.concurrency)
                async def one(member):
                    async with sem:
                        conn=await self._connect()
                        try:
                            if not await self._alive(conn,batch_id):return
                            await conn.execute("UPDATE credentials SET status='processing',error_msg='',updated_at=? WHERE id=?",(utcnow_iso(),member['credential_id']))
                            await conn.commit()
                            await self._run_member(conn,batch_id,member,step,batch)
                        except Exception as member_error:
                            stage={'qualification':'qual_failed','upi':'upi_failed','sms':'bind_failed'}[step]
                            message=f'{type(member_error).__name__}: {safe_error(str(member_error))}'
                            await self._set_stage(conn,batch_id,member['credential_id'],stage,message)
                            await log_runtime(conn,f"任务 #{batch_id} / {member['account']}：{message}",level='error',batch_id=batch_id)
                        finally:await conn.close()
                await asyncio.gather(*(one(member) for member in members))
            if not await self._alive(db,batch_id):return
            await self._save_progress(db,batch_id)
            members=await self._members(db,batch_id)
            final='completed' if members and all(m['stage'] in ('done','paid_without_sms') for m in members) else 'failed'
            await self._set_status(db,batch_id,final,'done')
            await db.execute('UPDATE task_batches SET completed_at=CURRENT_TIMESTAMP WHERE id=?',(batch_id,))
            await db.commit()
            await log_runtime(db,f"任务 #{batch_id}：执行结束，成功 {sum(m['stage']=='done' for m in members)} / {len(members)}",batch_id=batch_id)
            try:
                await write_task_report(db,batch_id)
            except Exception as report_error:
                await log_runtime(db,f"任务 #{batch_id}：报告写入失败（{type(report_error).__name__}），下载任务 MD 时可重试生成",level='error',batch_id=batch_id)
        except Exception as e:
            await self._set_status(db,batch_id,'failed','error')
            await self._save_progress(db,batch_id)
            await db.execute('UPDATE task_batches SET completed_at=CURRENT_TIMESTAMP WHERE id=?',(batch_id,));await db.commit()
            await log_runtime(db,f"任务 #{batch_id}：执行异常（{type(e).__name__}）",level='error',batch_id=batch_id)
            try:await write_task_report(db,batch_id)
            except Exception as report_error:
                await log_runtime(db,f"任务 #{batch_id}：报告写入失败（{type(report_error).__name__}），可稍后重试生成",level='error',batch_id=batch_id)
        finally:
            await db.close()
            _RUNNING.pop((self.db_path,batch_id),None)

    async def _run_member(self,db,bid,m,step,batch):
        cid=m['credential_id']
        account=m['account'] or f'账号 #{cid}'
        async def keep_running():return await self._alive(db,bid)
        async def progress(message):await log_runtime(db,f"任务 #{bid} / {account}：{message}",batch_id=bid)
        if step=='qualification':
            svc=QualificationService(db=db,**self.qual)
            outcome=await svc.check_single(m['at_snapshot'] or '')
            await db.execute('INSERT INTO qualification_checks(credential_id,at_token,has_qualification,raw_response) VALUES(?,?,?,?)',
                             (cid,m['at_snapshot'] or '',int(outcome.has_qualification),json.dumps(outcome.raw,ensure_ascii=False)))
            await db.commit()
            await self._set_stage(db,bid,cid,'qualified' if outcome.has_qualification else 'qual_failed', '' if outcome.has_qualification else outcome.reason)
            await progress('资格通过' if outcome.has_qualification else '资格未通过，停止后续提链和接码')
        elif step=='upi':
            snapshot=json.loads(batch['snapshot_json'] or '{}')
            options=dict(self.upi)
            if snapshot.get('proxy_enabled') and snapshot.get('proxies'):
                # 账号稳定分配一条代理，原订单恢复时不再次建单或切换代理。
                options['ytc_proxies']=[snapshot['proxies'][(cid-1)%len(snapshot['proxies'])]]
            svc=UPIService(db=db,**options)
            if hasattr(svc,'provider'):
                svc.provider.should_continue=keep_running;svc.provider.on_progress=progress
            task=await svc.create_with_retries(cid,batch['amount'] or 10000,batch['note'] or '',at_token=m['at_snapshot'])
            if task.get('status')=='paused':
                await self._set_status(db,bid,'paused')
                await progress('原支付订单等待确认，任务已暂停；继续任务将查询原订单')
                return
            good=task['status']=='success'
            await self._set_stage(db,bid,cid,'paid' if good else 'upi_failed', '' if good else task.get('error_msg','提链支付失败'),upi_task_id=task.get('id'))
            await progress('提链支付成功，进入接码' if good else '提链支付失败，停止接码')
        elif step=='sms':
            svc=SMSService(db=db,**{**self.sms,'max_retries':None})
            svc.should_continue=keep_running;svc.on_progress=progress
            await self._set_stage(db,bid,cid,'binding')
            task=await svc.bind_with_retries(cid)
            if task['status']=='paused':return
            good=task['status']=='bound'
            await self._set_stage(db,bid,cid,'done' if good else 'bind_failed','' if good else task.get('error_msg','接码未完成'),sms_task_id=task.get('id'))
            if good:
                await db.execute("UPDATE credentials SET status='done',error_msg='',updated_at=? WHERE id=?",(utcnow_iso(),cid));await db.commit()
                snapshot=json.loads(batch['snapshot_json'] or '{}')
                if snapshot.get('integration_enabled'):
                    from .integration_service import enqueue_push
                    try:await enqueue_push(db,cid,bid,snapshot)
                    except Exception:
                        await log_runtime(db,f'任务 #{bid} / {account}：本地成功，集成推送暂不可用，请在集成页面核对',level='error',batch_id=bid)
            await progress('接码绑定成功，全流程完成' if good else '接码未完成')

    async def pause(self,batch_id):
        db=await self._connect()
        try:
            await self._set_status(db,batch_id,'paused')
            await log_runtime(db,f"任务 #{batch_id}：已请求暂停，当前外部订单保留",batch_id=batch_id)
        finally:await db.close()

    async def resume(self,batch_id):
        if (self.db_path,batch_id) in _RUNNING and not _RUNNING[(self.db_path,batch_id)].done():
            raise ValueError('任务正在收尾暂停，请稍后再继续')
        db=await self._connect()
        try:
            async with db.execute('SELECT status FROM task_batches WHERE id=?',(batch_id,)) as cur:row=await cur.fetchone()
            if not row or row['status']!='paused':raise ValueError('仅暂停任务可继续')
            await self._set_status(db,batch_id,'running')
        finally:await db.close()
        self.start(batch_id)

    async def retry_failed(self,batch_id):
        result=await self.create_retry_batch(batch_id)
        self.start(result['batch_id'])
        return result

    async def create_retry_batch(self,batch_id):
        db=await self._connect()
        try:
            await db.execute('BEGIN IMMEDIATE')
            async with db.execute('SELECT * FROM task_batches WHERE id=?',(batch_id,)) as cur:source=await cur.fetchone()
            if not source:raise ValueError('任务不存在')
            if source['status'] not in ('completed','failed'):raise ValueError('请等待任务执行结束后再重试失败账号')
            members=await self._members(db,batch_id,{'qual_failed','upi_failed','bind_failed','pending','qualified','paid','binding'})
            if not members:raise ValueError('没有可重试的失败项')
            base=re.sub(r'-重试(?:-\d+)?$','',source['name']);base=re.sub(r'\d{8}$','',base).strip() or '任务'
            prefix=base+datetime.now().strftime('%Y%m%d')+'-重试'
            async with db.execute('SELECT name FROM task_batches') as cur:names={r['name'] async for r in cur}
            name,number=prefix,2
            while name in names:name=f'{prefix}-{number}';number+=1
            # 新流程已支付的接码失败项从 paid 继续，不重新扣提链额度。
            stages=['paid' if m['stage'] in ('bind_failed','paid','binding') and source['workflow_version']>=2 else 'pending' for m in members]
            snapshot=json.loads(source['snapshot_json']) if source['snapshot_json'] else self._legacy_snapshot()
            async with db.execute("INSERT INTO task_batches(name,total_credentials,status,current_step,progress_json,amount,note,source_batch_id,workflow_version,snapshot_json)"
                                  " VALUES(?,?,'running','created',?,?,?,?,?,?)",
                                  (name,len(members),json.dumps(_counts(stages)),source['amount'] or 10000,source['note'] or '',batch_id,
                                   max(3,source['workflow_version']),json.dumps(snapshot))) as cur:new_id=cur.lastrowid
            for m,stage in zip(members,stages):
                await db.execute('INSERT INTO batch_members(batch_id,credential_id,stage,at_snapshot,upi_task_id) VALUES(?,?,?,?,?)',
                                 (new_id,m['credential_id'],stage,m['at_snapshot'],m['upi_task_id'] if stage=='paid' else None))
            await db.commit()
            return {'batch_id':new_id,'name':name,'source_batch_id':batch_id}
        finally:await db.close()

    async def get_progress(self,batch_id):
        db=await self._connect()
        try:
            async with db.execute('SELECT * FROM task_batches WHERE id=? AND deleted_at IS NULL',(batch_id,)) as cur:row=await cur.fetchone()
            if not row:return None
            result=dict(row)
            result['members']=[{k:v for k,v in m.items() if k!='at_snapshot'} for m in await self._members(db,batch_id)]
            snapshot=json.loads(result.pop('snapshot_json',None) or '{}')
            result.update({key:snapshot.get(key, key=='sms_enabled') for key in ('sms_enabled','proxy_enabled','integration_enabled')})
            return result
        finally:await db.close()
