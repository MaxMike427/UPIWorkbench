"""M5 提链编排：DB 落库 + provider 调度（ytc 主 / nerver 备）。"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import httpx

from ..utils.helpers import utcnow_iso
from .upi_providers import (ExecutionPaused, LocalUPIProvider, MasiPaymentProvider,
    NerverUPIProvider, WangyouUPIProvider, XxsyunPaymentProvider, YtcUPIProvider)
from .report_service import safe_error


@dataclass
class UPIBatchResult:
    total: int = 0
    success: int = 0
    failed: int = 0
    details: list[dict] = field(default_factory=list)


class UPIService:
    def __init__(self, db, provider: str = "ytc",
                 extract_provider: str | None = None, payment_provider: str | None = None,
                 ytc_base_url: str = "https://ytc.xxsyun.xyz", ytc_api_key: str = "",
                 payment_base_url: str = "https://masi.cc.cd", payment_cdk: str = "",
                 nerver_base_url: str = "https://cha.nerver.cc", nerver_api_key: str = "",
                 promo_id: str = "plus-1-month-free",
                 pool_keep: int = 50, pool_max: int = 2000,
                 timeout: float = 600.0, concurrency: int = 3,
                 ytc_poll_interval: float = 10.0,
                 max_retries: int = 5, retry_interval: float = 120.0,
                 ytc_proxy_source: str = "auto",
                  ytc_payment_cdk: str = "", payment_api_key: str = "",
                 wait_for_payment: bool = False,
                 ytc_proxies: list[str] | None = None,
                 # 兼容旧构造（nerver 直参）
                 base_url: str | None = None, api_key: str | None = None):
        self.db = db
        if base_url is not None:  # 旧式：base_url/api_key 即 nerver
            provider = "nerver"
            nerver_base_url = base_url
            if api_key is not None:
                nerver_api_key = api_key
        self.provider_name = provider
        self.extract_provider = extract_provider or provider
        self.payment_provider = payment_provider or provider
        self.payment_adapter = None
        integrated_wait = wait_for_payment and self.payment_provider == self.extract_provider
        if provider == "ytc":
            if not ytc_api_key:
                raise RuntimeError("upi_provider.api_key（ohk_）未配置")
            self.provider = YtcUPIProvider(ytc_base_url, ytc_api_key, timeout=timeout,
                                           poll_interval=ytc_poll_interval, wait_for_payment=integrated_wait,
                                           proxies=ytc_proxies, proxy_source=ytc_proxy_source)
        elif provider == "nerver":
            self.provider = NerverUPIProvider(nerver_base_url, nerver_api_key, promo_id,
                                              pool_keep, pool_max, timeout=timeout)
        elif provider == "wangyou":
            if not payment_cdk:
                raise RuntimeError("请先保存忘忧 UPI CDK")
            self.provider = WangyouUPIProvider(ytc_base_url, payment_cdk,
                                               entry_proxies=ytc_proxies or [], exit_proxies=ytc_proxies or [],
                                               timeout=timeout)
        elif provider == "local":
            self.provider = LocalUPIProvider(base_url=ytc_base_url or nerver_base_url,
                                             proxies=ytc_proxies or [], timeout=timeout,
                                             extract_func=None)
        else:
            raise ValueError(f"未知提链 provider: {provider}")
        if self.payment_provider == "masi":
            if not payment_cdk:
                raise RuntimeError("请先保存 Masi 支付 CDK")
            self.payment_adapter = MasiPaymentProvider(payment_base_url, payment_cdk, timeout=timeout)
        elif self.payment_provider == "xxsyun_upi":
            if not (payment_api_key or payment_cdk):
                raise RuntimeError("请先保存 upi.xxsyun API Key（ohk_）")
            self.payment_adapter = XxsyunPaymentProvider(payment_base_url, payment_api_key or payment_cdk, timeout=timeout)
        elif self.payment_provider not in (self.extract_provider, "ytc", "nerver", "wangyou"):
            raise ValueError(f"未知或未接入支付 provider: {self.payment_provider}")
        self.concurrency = max(1, concurrency)
        self.max_retries = max(1, max_retries)
        self.retry_interval = max(0.0, retry_interval)
        self.wait_for_payment = integrated_wait

    async def create_upi_link(self, credential_id: int, amount: int,
                              note: str = "", callback_url: str = "",
                              sms_task_id: int | None = None, at_token: str | None = None,
                              account_email: str = "") -> dict:
        async with self.db.execute(
                "SELECT id, account, COALESCE(access_token,'') AS at FROM credentials WHERE id=?",
                (credential_id,)) as cur:
            cred = await cur.fetchone()
        if not cred:
            raise ValueError(f"credential {credential_id} 不存在")
        active = None
        if self.provider_name in ('ytc', 'wangyou') or self.payment_provider == 'xxsyun_upi':
            async with self.db.execute("SELECT id,external_job_id,external_job_secret,upi_link,payment_order_id FROM upi_tasks WHERE credential_id=?"
                                       " AND status='processing' AND (external_job_id IS NOT NULL OR payment_order_id IS NOT NULL)"
                                       " ORDER BY id DESC LIMIT 1",(credential_id,)) as cur:
                active=await cur.fetchone()
        if active:
            task_id=active['id']
        else:
            async with self.db.execute(
                "INSERT INTO upi_tasks(credential_id, sms_task_id, amount, note, callback_url, status)"
                " VALUES(?,?,?,?,?, 'pending')",
                (credential_id, sms_task_id, amount, note, callback_url)) as cur:
                task_id = cur.lastrowid
            await self.db.commit()

        async def _fail(reason: str):
            await self.db.execute(
                "UPDATE upi_tasks SET status='failed', error_msg=?, updated_at=? WHERE id=?",
                (reason[:500], utcnow_iso(), task_id))
            await self.db.commit()
            return await self._get(task_id)

        token = cred["at"] if at_token is None else at_token
        account_email = account_email or cred["account"] or ""
        if not (token or "").strip():
            return await _fail("empty-token")
        await self.db.execute("UPDATE upi_tasks SET status='processing' WHERE id=?", (task_id,))
        await self.db.commit()
        try:
            existing_link = active['upi_link'] if active and self.payment_provider == 'xxsyun_upi' else ''
            if existing_link:
                link, raw = existing_link, {"resumed_payment_order": active['payment_order_id']}
            elif self.provider_name in ('ytc', 'wangyou'):
                async def submitted(jid):
                    if isinstance(jid, dict):
                        await self.db.execute('UPDATE upi_tasks SET external_job_id=?,external_job_secret=? WHERE id=?',
                                               (jid.get('job_id',''),jid.get('job_secret',''),task_id))
                    else:
                        await self.db.execute('UPDATE upi_tasks SET external_job_id=? WHERE id=?',(jid,task_id))
                    await self.db.commit()
                self.provider.on_submitted=submitted
                kwargs={'job_id':active['external_job_id'] if active else None}
                if self.provider_name == 'wangyou':
                    async with self.db.execute('SELECT external_job_secret FROM upi_tasks WHERE id=?',(task_id,)) as cur:
                        current=await cur.fetchone()
                    kwargs['job_secret']=current['external_job_secret'] if current else None
                if self.provider_name=='local':kwargs['account_email']=account_email
                link,raw=await self.provider.fetch_link(token,self.concurrency,**kwargs)
            else:
                kwargs={}
                if self.provider_name=='local':kwargs['account_email']=account_email
                link, raw = await self.provider.fetch_link(token, self.concurrency, **kwargs)
        except ExecutionPaused as e:
            return {**(await self._get(task_id)), 'status':'paused', 'error_msg':str(e)}
        except Exception as e:
            if self.provider_name in ('ytc', 'wangyou') and isinstance(e,httpx.RequestError):
                saved=await self._get(task_id)
                if saved.get('external_job_id'):
                    return {**saved,'status':'paused','error_msg':'等待原支付订单状态，请继续任务核对'}
            return await _fail(str(e)[:500])
        if not link:
            return await _fail("no-next-action")
        await self.db.execute("UPDATE upi_tasks SET upi_link=?, updated_at=? WHERE id=?",
                              (link, utcnow_iso(), task_id))
        await self.db.commit()
        if self.payment_adapter is not None:
            self.payment_adapter.should_continue=getattr(self.provider, 'should_continue', None)
            self.payment_adapter.on_progress=getattr(self.provider, 'on_progress', None)
            try:
                payment_kwargs={}
                if isinstance(self.payment_adapter, XxsyunPaymentProvider):
                    async def payment_submitted(order_id):
                        await self.db.execute('UPDATE upi_tasks SET payment_order_id=? WHERE id=?',(order_id,task_id))
                        await self.db.commit()
                    self.payment_adapter.on_submitted=payment_submitted
                    async with self.db.execute('SELECT payment_order_id FROM upi_tasks WHERE id=?',(task_id,)) as cur:
                        payment_row=await cur.fetchone()
                    payment_kwargs['order_id']=payment_row['payment_order_id'] if payment_row else None
                    payment_kwargs['on_submitted']=payment_submitted
                payment=await self.payment_adapter.create_or_wait(link, token, account_email, **payment_kwargs)
            except Exception as exc:
                return await _fail(str(exc)[:500])
            if payment.get('status')=='paused':
                return {**(await self._get(task_id)), 'status':'paused', 'error_msg':'支付订单仍在处理中'}
            if payment.get('status')!='success':
                return await _fail(payment.get('error_msg') or '支付服务未完成')
            raw={**(raw if isinstance(raw,dict) else {}), 'payment':payment}
        await self.db.execute(
            "UPDATE upi_tasks SET status='success', upi_link=?, qr_code_data=?,"
            " raw_response=?, updated_at=? WHERE id=?",
            (link, (raw.get("upi_qr_data") or "") if isinstance(raw, dict) else "",
             json.dumps(raw, ensure_ascii=False)[:16000], utcnow_iso(), task_id))
        await self.db.commit()
        # 提链支付成功不等于全流程完成，账号 status=done 由最后接码步骤写入。
        return await self._get(task_id)

    async def create_with_retries(self, credential_id: int, amount: int,
                                    note: str = "", callback_url: str = "", at_token: str | None = None,
                                    account_email: str = "") -> dict:
        """单号自动重试：失败后等待 retry_interval，最多 max_retries 次。"""
        last: dict = {}
        progress=getattr(self.provider,'on_progress',None)
        for attempt in range(self.max_retries):
            running=getattr(self.provider,'should_continue',None)
            if running and not await running():
                return {**last,'status':'paused'}
            if attempt > 0:
                deadline=time.monotonic()+self.retry_interval
                while time.monotonic()<deadline:
                    if running and not await running():return {**last,'status':'paused'}
                    await asyncio.sleep(min(1.0,max(0,deadline-time.monotonic())))
            if progress:
                await progress(f'开始第 {attempt+1}/{self.max_retries} 次提链支付尝试')
            last = await self.create_upi_link(credential_id, amount, note, callback_url,
                                              at_token=at_token, account_email=account_email)
            if last.get("status") in ("success","paused"):
                return last
            if (last.get("error_msg") or "") == "empty-token":
                return last  # 无 AT，重试无意义
            if progress:
                suffix=f'，等待 {int(self.retry_interval)} 秒后重试' if attempt+1<self.max_retries else '，已达到本次任务尝试上限'
                await progress(f"本次提链未完成：{safe_error(last.get('error_msg',''))}{suffix}")
        return last

    async def _get(self, task_id: int) -> dict:
        async with self.db.execute("SELECT * FROM upi_tasks WHERE id=?", (task_id,)) as cur:
            return dict(await cur.fetchone())

    async def create_batch(self, credential_ids: list[int], amount: int,
                           note: str = "", callback_url: str = "") -> UPIBatchResult:
        sem = asyncio.Semaphore(self.concurrency)

        async def _one(cid: int):
            async with sem:
                try:
                    t = await self.create_with_retries(cid, amount, note, callback_url)
                    return {"credential_id": cid, "status": t["status"],
                            "upi_link": bool(t.get("upi_link")), "error": t.get("error_msg") or ""}
                except Exception as e:
                    return {"credential_id": cid, "status": "failed", "error": str(e)[:300]}

        details = await asyncio.gather(*(_one(cid) for cid in credential_ids))
        res = UPIBatchResult(total=len(details), details=list(details))
        for d in details:
            if d["status"] == "success":
                res.success += 1
            else:
                res.failed += 1
        return res

    async def retry_task(self, task_id: int) -> dict:
        async with self.db.execute("SELECT * FROM upi_tasks WHERE id=?", (task_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            raise ValueError("upi task 不存在")
        if row["status"] != "failed":
            raise ValueError("仅 failed 任务可重试")
        return await self.create_upi_link(row["credential_id"], row["amount"],
                                          row["note"] or "", row["callback_url"] or "",
                                          row["sms_task_id"])
