"""提链 provider：YtcUPIProvider（主，暗光提链）+ NerverUPIProvider（备）。

ytc 契约（前端 JS 逆向，见 INTEGRATION.md §3）：
- 登录：POST /api/auth/login {api_key: 'ohk_...'} → cookie 会话（同源 fetch，无显式头）
- 建单：POST /api/jobs {tokens(AT 换行), concurrency, rounds?, stop_after_first?,
  auto_submit:true, auto_refresh_expired:true, team_id?, proxy_source?} → {id}
- 轮询：GET /api/jobs/{id}/results；SSE：GET /api/jobs/{id}/events?summary=true
- 任务终态：succeeded/paid/link_ready（成功）/ cancelled/needs_review（需人工）
- 导出：GET /api/jobs/{id}/export；成功记录：POST /api/success-records
"""
from __future__ import annotations

import json
import hashlib
import time
import asyncio

try:
    from app.upi_zero_link.protocol import extract as _local_extract
except Exception:
    try:
        import sys
        sys.path.insert(0, '/tmp/opencode/upi-zero-link')
        from upi_zero_link.protocol import extract as _local_extract
    except Exception:
        _local_extract = None

import httpx

YTC_OK = {"succeeded", "paid", "link_ready"}
YTC_BAD = {"cancelled", "failed", "error"}
YTC_REVIEW = {"needs_review"}

YTC_STAGES = {
    "queued": "等待平台调度", "connecting": "连接与账号准备", "preflight": "账号预检",
    "checkout": "创建结账", "stripe_init": "初始化支付页", "tax_update": "核验税区与金额",
    "payment_method": "创建 UPI 委托", "stripe_confirm": "提交支付确认", "approve": "等待结账授权",
    "instructions": "读取支付链接", "completed": "提链完成", "succeeded": "提链完成",
    "needs_review": "待人工核对", "failed": "执行失败", "cancelled": "已取消",
}
YTC_PAYMENT_STATES = {
    "pending": "等待支付", "queued": "等待支付调度", "running": "支付执行中", "processing": "支付执行中",
    "submitted": "支付已提交，等待确认", "claimed": "支付执行中", "completed": "支付完成",
    "failed": "支付失败", "cancelled": "支付已取消", "needs_review": "支付待人工核对",
}


class ExecutionPaused(RuntimeError):
    """外部订单仍存在，停止本地等待但不可重新提交。"""


def select_ytc_api_key(*values: str) -> str:
    """优先选择 ytc 的 ohk_ API Key，避免误把其它提炼 CDK 当成登录凭证。"""
    values=[str(value or '').strip() for value in values]
    for value in values:
        if value.lower().startswith('ohk_'):
            return value
    return next((value for value in values if value), '')


def _find_link(obj, depth: int = 0):
    """在 results JSON 里找支付链接。ytc 实测：items[].url；其它键走通用搜索。"""
    if isinstance(obj, dict):
        items = obj.get("items")
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict) and isinstance(it.get("url"), str) and it["url"].startswith("http"):
                    return it["url"]
    found: list[str] = []
    good: list[str] = []

    def _walk(o, key: str = ""):
        if isinstance(o, dict):
            for k, v in o.items():
                _walk(v, str(k))
        elif isinstance(o, list):
            for v in o:
                _walk(v, key)
        elif isinstance(o, str) and o.startswith("http"):
            found.append(o)
            if any(w in key.lower() for w in ("link", "url", "upi", "pay", "checkout", "qr")):
                good.append(o)

    _walk(obj)
    return (good or found or [""])[0]




class LocalUPIProvider:
    """本地零元提链：复用 upi-zero-link 协议，支持代理输入。"""
    def __init__(self, base_url="", proxies=None, timeout=600.0, extract_func=None):
        self.base_url=base_url
        self.proxies=list(proxies or [])
        self.timeout=timeout
        self.extract_func=extract_func or _local_extract
        self.should_continue=None
        self.on_progress=None
        self.on_submitted=None

    async def fetch_link(self, at, concurrency=1, job_id=None, account_email=""):
        if self.extract_func is None:
            raise RuntimeError("本地提炼依赖未安装（curl_cffi/upi-zero-link）")
        import asyncio
        account={"email":account_email or "","access_token":at.strip(),"session_token":""}
        proxy=self.proxies[0] if self.proxies else ""
        result=await asyncio.to_thread(self.extract_func, account, proxy)
        if isinstance(result, dict) and result.get("ok"):
            link=result.get("url") or result.get("hosted_instructions_url") or ""
            if not link:
                raise RuntimeError("local link missing")
            if self.on_submitted:
                await self.on_submitted("local")
            return link, {"provider":"local","result":result}
        reason=result.get("error") if isinstance(result, dict) else "local failed"
        upstream=result.get("upstream_code") if isinstance(result, dict) else ""
        if upstream:
            reason=f"{reason} ({upstream})"
        raise RuntimeError(str(reason))


class MasiPaymentProvider:
    """Masi 扫码支付适配器：提交 Stripe UPI 链接并轮询订单终态。"""
    def __init__(self, base_url: str, cdk: str, timeout: float = 600.0,
                 poll_interval: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.cdk = cdk or ""
        self.timeout = timeout
        self.poll_interval = max(0.0, poll_interval)
        self.should_continue = None
        self.on_progress = None

    async def create_or_wait(self, link: str, access_token: str, email: str = "", order_id: str | None = None,
                             on_submitted=None) -> dict:
        headers = {"Content-Type": "application/json", "User-Agent": "UPIWorkbench/1.2"}
        body = {"ticket": self.cdk, "link": link, "access_token": access_token,
                "email": email, "require_online_workers": True}
        async with httpx.AsyncClient() as client:
            response = await client.post(f"{self.base_url}/api/scan-orders", json=body,
                                         headers=headers, timeout=30.0)
            try:
                data = response.json()
            except Exception as exc:
                raise RuntimeError(f"支付服务返回非 JSON（HTTP {response.status_code}）") from exc
            if response.status_code not in (200, 201) or not isinstance(data, dict) or not data.get("ok"):
                raise RuntimeError(str(data.get("error") or f"支付订单创建失败（HTTP {response.status_code}）")[:300])
            order = data.get("order") or {}
            order_id = str(order.get("order_id") or "")
            order_secret = str(data.get("order_secret") or "")
            if not order_id or not order_secret:
                raise RuntimeError("支付服务未返回订单凭据")
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                if self.should_continue and not await self.should_continue():
                    return {"status": "paused", "order_id": order_id}
                await asyncio.sleep(self.poll_interval)
                query = await client.get(f"{self.base_url}/api/orders/{order_id}",
                                         headers={"X-Order-Secret": order_secret,
                                                  "User-Agent": headers["User-Agent"]},
                                         timeout=30.0)
                try:
                    queried = query.json()
                except Exception:
                    queried = {}
                current = queried.get("order") if isinstance(queried, dict) else {}
                current = current if isinstance(current, dict) else {}
                status = str(current.get("status") or queried.get("status") or "").lower()
                if self.on_progress:
                    await self.on_progress(f"支付服务：订单 {status or '处理中'}")
                if status == "completed":
                    return {"status": "success", "order_id": order_id, "raw": queried}
                if status in {"failed", "expired", "cancelled"}:
                    return {"status": "failed", "order_id": order_id,
                            "error_msg": str(current.get("message") or current.get("error") or status)[:300],
                            "raw": queried}
            return {"status": "failed", "order_id": order_id, "error_msg": "支付订单查询超时"}


class XxsyunPaymentProvider:
    """Camellia/OrderHub 支付：Bearer ytc API Key + /api/v1/orders。"""
    def __init__(self, base_url: str, api_key: str, timeout: float = 600.0, poll_interval: float = 5.0):
        self.base_url = base_url.rstrip("/")
        if self.base_url.endswith('/api/v1'): self.base_url=self.base_url[:-7]
        self.api_key = api_key or ""
        self.timeout = timeout
        self.poll_interval = max(0.0, poll_interval)
        self.should_continue = None
        self.on_progress = None

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    @staticmethod
    def _order_id(data):
        for item in data.get("created", []) if isinstance(data, dict) else []:
            order = item.get("order") if isinstance(item, dict) else {}
            if isinstance(order, dict) and order.get("id"): return str(order["id"])
        for item in data.get("duplicated", []) if isinstance(data, dict) else []:
            order = item.get("order") if isinstance(item, dict) else {}
            if isinstance(order, dict) and order.get("id"): return str(order["id"])
        return ""

    async def create_or_wait(self, link: str, access_token: str, email: str = "", order_id: str | None = None,
                             on_submitted=None) -> dict:
        if not self.api_key:
            return {"status": "failed", "error_msg": "upi.xxsyun 支付服务需要 ytc ohk_ API Key"}
        async with httpx.AsyncClient() as client:
            if not order_id:
                body={"items":[{"at":access_token.strip(),"link":link}],"dispatch_mode":"auto"}
                key=hashlib.sha256(f"{access_token}\n{link}".encode()).hexdigest()[:32]
                response=await client.post(f"{self.base_url}/api/v1/orders/batch", json=body,
                                           headers={**self._headers(),"Idempotency-Key":f"upi-workbench-{key}"}, timeout=30.0)
                try:data=response.json()
                except Exception:data={}
                if response.status_code >= 400 or not isinstance(data,dict) or not data.get("ok"):
                    error=data.get("error") if isinstance(data,dict) else ""
                    if isinstance(error,dict): error=error.get("message") or error.get("code") or ""
                    return {"status":"failed","error_msg":str(error or f"支付订单创建失败（HTTP {response.status_code}）")[:300]}
                order_id=self._order_id(data)
                if not order_id:return {"status":"failed","error_msg":"支付服务未返回订单 ID"}
                if on_submitted: await on_submitted(order_id)
            deadline=time.monotonic()+self.timeout
            while time.monotonic()<deadline:
                if self.should_continue and not await self.should_continue():return {"status":"paused","order_id":order_id}
                await asyncio.sleep(self.poll_interval)
                query=await client.get(f"{self.base_url}/api/v1/orders/{order_id}",headers=self._headers(),timeout=30.0)
                try:payload=query.json()
                except Exception:payload={}
                order=payload.get("order") if isinstance(payload,dict) else {}
                order=order if isinstance(order,dict) else {}
                status=str(order.get("status") or payload.get("status") or "").lower()
                if self.on_progress:await self.on_progress(f"upi.xxsyun：订单 {status or '处理中'}")
                if status in ("completed","success","paid"):
                    return {"status":"success","order_id":order_id,"raw":payload}
                if status in ("failed","cancelled","expired","rejected"):
                    return {"status":"failed","order_id":order_id,"error_msg":str(order.get("failure_reason") or order.get("error") or status)[:300],"raw":payload}
            return {"status":"failed","order_id":order_id,"error_msg":"支付订单查询超时"}


class WangyouUPIProvider:
    """忘忧公益提链：checkout 任务以 job_secret 鉴权并异步查询结果。"""
    def __init__(self, base_url: str, cdk: str = "", entry_proxies=None, exit_proxies=None,
                 timeout: float = 1800.0, poll_interval: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.cdk = cdk or ""
        self.entry_proxies = list(entry_proxies or [])
        self.exit_proxies = list(exit_proxies or [])
        self.timeout = timeout
        self.poll_interval = max(0.0, poll_interval)
        self.should_continue = None
        self.on_progress = None
        self.on_submitted = None

    async def _progress(self, client, job_id, secret):
        response = await client.get(f"{self.base_url}/api/checkout-progress", params={"job_id": job_id},
                                    headers={"X-Pay153-Job-Secret": secret}, timeout=30.0)
        try: data = response.json()
        except Exception: data = {}
        if response.status_code >= 400 or not isinstance(data, dict):
            raise RuntimeError(f"wangyou 查询任务失败（HTTP {response.status_code}）")
        return data

    @staticmethod
    def _link(result):
        result = result if isinstance(result, dict) else {}
        return (result.get("short_link") or result.get("provider_redirect_url") or
                result.get("checkout_url") or result.get("qr_data") or "")

    async def fetch_link(self, at: str, concurrency: int = 5, job_id: str | None = None,
                         job_secret: str | None = None) -> tuple[str, dict]:
        async with httpx.AsyncClient() as client:
            if not job_id:
                body = {"token": at.strip(), "entry_proxies": self.entry_proxies,
                        "exit_proxies": self.exit_proxies, "cdk": self.cdk,
                        "plan": "plus", "link_type": "upi", "retry_count": max(1, concurrency),
                        "use_promo": True, "upi_vpa": ""}
                if not self.cdk: body.pop("cdk")
                response = await client.post(f"{self.base_url}/api/checkout", json=body,
                                             headers={"Content-Type": "application/json"}, timeout=30.0)
                try: created = response.json()
                except Exception: created = {}
                if response.status_code != 202 or not isinstance(created, dict) or not created.get("job_id"):
                    raise RuntimeError(str(created.get("error") or f"wangyou 创建任务失败（HTTP {response.status_code}）")[:300])
                job_id = str(created["job_id"])
                job_secret = str(created.get("job_secret") or "")
                if not job_secret: raise RuntimeError("wangyou 未返回任务凭据")
                if self.on_submitted: await self.on_submitted({"job_id": job_id, "job_secret": job_secret})
            elif not job_secret:
                raise RuntimeError("wangyou 原任务缺少 job_secret，不能安全继续查询")
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                if self.should_continue and not await self.should_continue():
                    raise ExecutionPaused("wangyou 原任务仍在执行，任务已请求暂停")
                data = await self._progress(client, job_id, job_secret)
                status = str(data.get("status") or "").lower()
                result = data.get("result") if isinstance(data.get("result"), dict) else {}
                if self.on_progress:
                    await self.on_progress(f"wangyou：{data.get('text') or status or '查询任务进度'}")
                link = self._link(result)
                if link and status in ("done", "error", "cancelled"):
                    if status != "done": raise RuntimeError(str(data.get("error") or "wangyou 任务失败")[:300])
                    return link, {"provider": "wangyou", "job_id": job_id, "job_secret": job_secret,
                                  "status": status, "result": result}
                if status in ("error", "cancelled"):
                    raise RuntimeError(str(data.get("error") or f"wangyou 任务 {status}")[:300])
                await asyncio.sleep(self.poll_interval)
            raise ExecutionPaused("wangyou 任务查询超时，请继续查询原任务，不重复提交")

class YtcUPIProvider:
    def __init__(self, base_url: str, api_key: str, timeout: float = 600.0,
                 poll_interval: float = 10.0, wait_for_payment: bool = False,
                 proxies:list[str]|None=None, proxy_source: str = 'auto'):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.wait_for_payment = wait_for_payment
        self.proxies=proxies or []
        self.proxy_source='internal' if proxy_source=='provider' else proxy_source if proxy_source in ('auto','internal','provided') else 'auto'
        self.should_continue = None
        self.on_progress = None
        self.on_submitted = None
        self.progress_heartbeat = 30.0
        self._progress_signature = None
        self._progress_time = None

    async def _emit_progress(self, message: str, signature):
        """字段变化立即输出；未变化时每 30 秒心跳，避免终端看起来卡住或刷屏。"""
        if not self.on_progress:
            return
        now = time.monotonic()
        if signature != self._progress_signature or self._progress_time is None or now-self._progress_time >= self.progress_heartbeat:
            await self.on_progress(message)
            self._progress_signature, self._progress_time = signature, now

    async def _task_progress(self, client: httpx.AsyncClient, job_id: str, job: dict):
        if not self.on_progress:
            return
        try:
            resp = await client.get(f"{self.base_url}/api/tasks", params={"job_id":job_id,"page":1,"limit":10}, timeout=15.0)
            if resp.status_code >= 400:
                raise ValueError("progress unavailable")
            data = resp.json()
            tasks = [item.get("task") or {} for item in data.get("items",[]) if isinstance(item,dict)]
            if not tasks:
                state = job.get("status","")
                await self._emit_progress("ytc：等待平台任务进度，仍在查询原订单", ("empty",state))
                return
            for task in tasks:
                stage = str(task.get("stage") or task.get("status") or "")
                def number(key, default=0):
                    value = task.get(key)
                    return max(0,int(value)) if isinstance(value,(int,float)) else default
                pct = min(100,number("progress"))
                current, maximum, elapsed = number("current_round"), number("max_rounds"), number("elapsed_seconds")
                text = f"ytc：{YTC_STAGES.get(stage,'平台处理中')} · {pct}%"
                if maximum:
                    text += f" · 第 {current}/{maximum} 轮"
                text += f" · 已耗时 {elapsed} 秒"
                # 不使用 account/token/error/代理 IP 等原始字段；所属账号由本地回调添加。
                await self._emit_progress(text, ("task",str(task.get("id","")),stage,pct,current,maximum))
        except (httpx.RequestError, ValueError, TypeError, AttributeError):
            await self._emit_progress("ytc：平台进度暂不可用，继续查询原订单", ("progress-unavailable",))

    async def _login(self, client: httpx.AsyncClient):
        resp = await client.post(f"{self.base_url}/api/auth/login",
                                 json={"api_key": self.api_key}, timeout=30.0)
        data = resp.json()
        if isinstance(data, dict) and data.get("ok") is False:
            raise RuntimeError("ytc 账户认证失败，请检查 CDK")
        if not isinstance(data,dict) or resp.status_code >= 400 or data.get("authenticated") is False:
            raise RuntimeError("ytc 账户认证失败，请检查服务端 API Key")

    async def create_job(self, client: httpx.AsyncClient, at: str,
                          concurrency: int = 5) -> str:
        body={"tokens":at.strip(),"concurrency":concurrency,"auto_submit":True,"auto_refresh_expired":True}
        source=self.proxy_source
        if source=='auto':source='provided' if self.proxies else 'internal'
        body['proxy_source']=source
        if source=='provided':
            if not self.proxies:
                raise RuntimeError('ytc 已选择 provided，但本地代理库为空')
            body['proxies']='\n'.join(self.proxies)
        resp = await client.post(
            f"{self.base_url}/api/jobs",
            json=body,
            timeout=30.0,
        )
        data = resp.json()
        jid = (data or {}).get("id") if isinstance(data, dict) else None
        if not jid:
            reason=data.get('error') or data.get('reason') if isinstance(data,dict) else ''
            if reason in ('provided_proxy_required','invalid_proxy','internal_proxy_unavailable'):
                raise RuntimeError('ytc 代理配置无效或不可用，请检查代理库')
            raise RuntimeError(f"ytc 创建任务被拒绝（HTTP {resp.status_code}），请检查额度、CDK 和代理")
        return str(jid)

    async def poll_results(self, client: httpx.AsyncClient, job_id: str) -> dict:
        """轮询批次摘要至终态，成功后取 results。返回 {'job':..., 'results':...}。"""
        deadline = time.monotonic() + self.timeout
        job: dict = {}
        while time.monotonic() < deadline:
            import asyncio

            if self.should_continue and not await self.should_continue():
                raise ExecutionPaused("任务已暂停，外部提链订单保留")
            await asyncio.sleep(self.poll_interval)
            try:
                resp = await client.get(f"{self.base_url}/api/jobs/{job_id}?summary=true",timeout=30.0)
            except httpx.RequestError:
                if self.wait_for_payment:
                    await self._emit_progress("ytc：查询网络异常，继续等待原订单，不重复提交", ("network",))
                    await asyncio.sleep(max(1,self.poll_interval))
                    continue
                raise
            data = resp.json()
            job = data if isinstance(data, dict) else {}
            st = str(job.get("status", "")).lower()
            await self._task_progress(client,job_id,job)
            if st in YTC_OK:
                break
            if st in YTC_BAD or st in YTC_REVIEW:
                return {"job": job, "results": {}, "status": st,
                        "task_error": await self._task_error(client, job_id)}
        else:
            if self.wait_for_payment:
                raise ExecutionPaused("ytc 订单等待超时，请继续查询原订单，不重新支付")
            raise TimeoutError("ytc poll timeout")
        resp = await client.get(f"{self.base_url}/api/jobs/{job_id}/results",
                                timeout=30.0)
        results = resp.json()
        return {"job": job, "results": results if isinstance(results, dict) else {},
                "status": str(job.get("status", "")).lower()}

    async def _task_error(self, client: httpx.AsyncClient, job_id: str) -> str:
        """取首个 task 的错误码/阶段，供 error_msg 使用。"""
        try:
            resp = await client.get(f"{self.base_url}/api/tasks",
                                    params={"job_id": job_id}, timeout=30.0)
            data = resp.json()
            items = (data or {}).get("items") if isinstance(data, dict) else None
            if items:
                t = (items[0] or {}).get("task", {}) or {}
                err = t.get("error") or {}
                code = err.get("code") or err.get("message") or ""
                stage = t.get("failure_stage") or t.get("stage") or ""
                return f"{code}@{stage}"[:200]
        except Exception:
            pass
        return ""

    @staticmethod
    def _job_status(results: dict) -> str:
        for k in ("status", "job_status", "state"):
            if results.get(k):
                return str(results[k]).lower()
        tasks = results.get("tasks") or results.get("items") or []
        if tasks and isinstance(tasks, list):
            st = str((tasks[0] or {}).get("status", "")).lower()
            if st:
                return st
        return ""

    async def fetch_link(self, at: str, concurrency: int = 5, job_id: str | None = None) -> tuple[str, dict]:
        """完整链路：登录→建单→轮询→提链接。返回 (link, raw)。"""
        async with httpx.AsyncClient() as client:
            await self._emit_progress("ytc：正在登录提链平台",("login",))
            await self._login(client)
            jid = job_id or await self.create_job(client, at, concurrency)
            await self._emit_progress("ytc：已连接原订单，开始查询执行进度" if job_id else "ytc：提链订单已提交，开始查询执行进度",("submitted",jid))
            if self.on_submitted:
                await self.on_submitted(jid)
            try:
                polled = await self.poll_results(client, jid)
            except TimeoutError:
                return "", {"job_id": jid, "timeout": True}
            st = polled.get("status", "")
            results = polled.get("results", {})
            if st in YTC_BAD or st in YTC_REVIEW:
                if st in YTC_REVIEW and self.wait_for_payment:
                    raise ExecutionPaused('ytc 原订单需要人工核对，保留订单，不重新支付')
                detail = polled.get("task_error") or ""
                # job.settings 可含代理认证 URI，错误信息只返回阶段与安全错误码。
                raise RuntimeError(f"ytc job {st} {detail}")
            link = _find_link(results)
            if link and self.wait_for_payment:
                results = await self._wait_payment(client,jid,results)
            return link, {"job_id": jid, "status": st, "results": results}

    async def _wait_payment(self,client,job_id,results):
        """提链后等待同一订单完成支付，不重复建单；可由任务暂停中断。"""
        started = time.monotonic()
        while True:
            items = results.get("items") or []
            states = tuple(str((item.get("submission") or {}).get("status") or "pending") for item in items)
            labels = "、".join(dict.fromkeys(YTC_PAYMENT_STATES.get(s,"等待支付确认") for s in states)) or "等待支付确认"
            await self._emit_progress(f"ytc：{labels} · 已等待支付 {int(time.monotonic()-started)} 秒",("payment",states))
            if items and all((item.get("submission") or {}).get("status")=="completed" for item in items):
                return results
            if any((item.get("submission") or {}).get("status") in ("failed","cancelled","needs_review") for item in items):
                raise ExecutionPaused("ytc 支付未成功，需要人工核对原订单，不自动重新支付")
            if self.should_continue and not await self.should_continue():
                raise ExecutionPaused("ytc 原订单仍在等待支付，任务已请求暂停")
            await asyncio.sleep(max(1,self.poll_interval))
            try:
                resp=await client.get(f"{self.base_url}/api/jobs/{job_id}/results",timeout=30.0)
            except httpx.RequestError:
                await self._emit_progress("ytc：支付查询网络异常，继续等待原订单",("payment-network",))
                continue
            if resp.status_code>=400:
                raise ExecutionPaused("ytc 支付状态查询失败，需要核对原订单")
            results=resp.json()


class NerverUPIProvider:
    """备选：cha.nerver.cc POST /api/v1/upi（公开文档，字段稳定）。"""

    def __init__(self, base_url: str, api_key: str = "", promo_id: str = "plus-1-month-free",
                 pool_keep: int = 50, pool_max: int = 2000, timeout: float = 300.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.promo_id = promo_id
        self.pool_keep = pool_keep
        self.pool_max = pool_max
        self.timeout = timeout

    async def fetch_link(self, at: str, concurrency: int = 5) -> tuple[str, dict]:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/api/v1/upi",
                json={"token": at.strip(), "promoId": self.promo_id,
                      "poolKeep": self.pool_keep, "poolMax": self.pool_max},
                headers=headers, timeout=self.timeout)
            data = resp.json()
        if not isinstance(data, dict) or not data.get("ok"):
            reason = str((data or {}).get("reason") or "unknown")
            raise RuntimeError(reason)
        link = data.get("upi_redirect_url") or data.get("upi_hosted_url") or ""
        return link, data
