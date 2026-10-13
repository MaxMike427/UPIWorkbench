"""代理库：校验 URI，存储真实凭证但只向页面返回脱敏地址。"""
from __future__ import annotations

import asyncio
import re
import ssl
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

PROBE_TARGET='https://api.ipify.org?format=json'
PROBE_TIMEOUT=6.0
PROBE_CONCURRENCY=8
DYNAMIC_SAMPLES=3
API_MAX_COUNT=200
API_MIN_INTERVAL=1.0
_last_api_fetch=0.0


def parse_proxy(value:str) -> dict:
    raw=value.strip()
    if raw.startswith('sk5://'):raw='socks5://'+raw[6:]
    if '://' not in raw:
        raw='http://'+raw
    if any(ord(char)<33 for char in raw):raise ValueError('代理地址包含无效字符')
    try:
        url=urlsplit(raw)
        if url.scheme not in ('http','https','socks4','socks5','socks5h') or not url.hostname or not url.port:
            raise ValueError('格式无效')
        if url.path not in ('','/') or url.query or url.fragment:
            raise ValueError('不支持路径或参数')
        if url.username is not None and url.password is None:raise ValueError('认证信息不完整')
        port=url.port
    except ValueError:
        raise ValueError('请填写有效的 HTTP/HTTPS/SOCKS4/SOCKS5 代理 URI 和端口')
    return {'url':urlunsplit((url.scheme,url.netloc,'','','')),'protocol':url.scheme,'host':url.hostname,'port':port}


def masked_proxy(value:str)->str:
    url=urlsplit(value)
    host=f'[{url.hostname}]' if ':' in (url.hostname or '') else url.hostname
    auth='****:****@' if url.username is not None else ''
    return f'{url.scheme}://{auth}{host}:{url.port}'


async def get_proxies(db)->dict:
    async with db.execute("SELECT value FROM configs WHERE key='proxy.enabled'") as cur:row=await cur.fetchone()
    async with db.execute('SELECT * FROM proxies ORDER BY id') as cur:
        items=[{'id':r['id'],'url':masked_proxy(r['url']),'protocol':r['protocol'],'host':r['host'],'port':r['port'],
                'is_dynamic':bool(r['is_dynamic'])} async for r in cur]
    return {'enabled':bool(row) and row['value']=='true','items':items,'total':len(items)}


async def save_proxies(db,text:str,enabled:bool)->dict:
    parsed=[];seen=set()
    for i,line in enumerate(text.splitlines(),1):
        if not line.strip():continue
        try:item=parse_proxy(line)
        except ValueError as e:raise ValueError(f'第 {i} 行：{e}')
        if item['url'] not in seen:parsed.append(item);seen.add(item['url'])
    if enabled and not parsed:raise ValueError('代理库开启时至少需要一个有效代理')
    await db.execute('DELETE FROM proxies')
    for item in parsed:
        await db.execute('INSERT INTO proxies(url,protocol,host,port) VALUES(?,?,?,?)',tuple(item[k] for k in ('url','protocol','host','port')))
    await db.execute("UPDATE configs SET value=?,updated_at=CURRENT_TIMESTAMP WHERE key='proxy.enabled'",('true' if enabled else 'false',))
    await db.commit()
    return await get_proxies(db)


async def append_proxies(db,text:str,enabled:bool,is_dynamic:bool=False)->dict:
    async with db.execute('SELECT url FROM proxies') as cur:existing={r['url'] async for r in cur}
    parsed=[];seen=set(existing)
    for i,line in enumerate(text.splitlines(),1):
        if not line.strip():continue
        try:item=parse_proxy(line)
        except ValueError as e:raise ValueError(f'第 {i} 行：{e}')
        if item['url'] not in seen:parsed.append(item);seen.add(item['url'])
    if enabled and not (existing or parsed):raise ValueError('代理库开启时至少需要一个有效代理')
    for item in parsed:
        await db.execute('INSERT INTO proxies(url,protocol,host,port,is_dynamic) VALUES(?,?,?,?,?)',
                         (*(item[k] for k in ('url','protocol','host','port')),1 if is_dynamic else 0))
    await db.execute("UPDATE configs SET value=?,updated_at=CURRENT_TIMESTAMP WHERE key='proxy.enabled'",('true' if enabled else 'false',))
    await db.commit()
    return await get_proxies(db)


async def set_dynamic(db,pid:int,is_dynamic:bool)->dict:
    async with db.execute('UPDATE proxies SET is_dynamic=? WHERE id=?',(1 if is_dynamic else 0,pid)) as cur:changed=cur.rowcount
    if not changed:raise ValueError('代理不存在')
    await db.commit()
    return await get_proxies(db)


class ApiBusy(ValueError):
    """上游 API 间隔不足，调用方应返回 429。"""


class ApiUpstream(ValueError):
    """上游 API 本身异常，调用方应返回 502。"""


def _public_http_url(url:str)->str:
    from urllib.parse import urlsplit as split
    from ipaddress import ip_address
    raw=url.strip()
    if not raw:raise ValueError('请填写代理 API 地址')
    if len(raw)>2000:raise ValueError('代理 API 地址过长')
    if any(ord(char)<33 for char in raw):raise ValueError('代理 API 地址包含无效字符')
    try:
        parts=split(raw)
        host=parts.hostname
    except ValueError:
        raise ValueError('代理 API 地址格式无效')
    if parts.scheme not in ('http','https') or not host:raise ValueError('代理 API 地址必须是 http 或 https')
    lowered=host.lower()
    if lowered in ('localhost','localhost.localdomain') or lowered.endswith(('.local','.internal')):raise ValueError('代理 API 地址不能指向本机或内网')
    try:ip_address(host)
    except ValueError:pass
    else:raise ValueError('代理 API 地址不能指向本机或内网')
    return raw


def _api_protocol(url:str,explicit:str|None)->str:
    if explicit:return explicit
    match=re.search(r'(?:^|[?&])proto(?:col)?=([a-z0-9]+)',url,re.I)
    if match and match.group(1).lower() in ('http','https','socks4','socks5'):return match.group(1).lower()
    return 'http'


async def fetch_api_proxies(db,url:str,protocol:str|None=None,is_dynamic:bool=True)->dict:
    global _last_api_fetch
    target=_public_http_url(url)
    scheme=_api_protocol(target,protocol)
    wait=API_MIN_INTERVAL-(time.monotonic()-_last_api_fetch)
    if wait>0:raise ApiBusy(f'请等待 {wait:.0f} 秒后再拉取，代理 API 有调用间隔限制')
    _last_api_fetch=time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=20.0,follow_redirects=False,trust_env=False) as client:
            response=await client.get(target,headers={'Accept':'text/plain'})
    except (httpx.HTTPError,ValueError):raise ApiUpstream('代理 API 暂时无法访问')
    if response.status_code>=400:raise ApiUpstream(f'代理 API 返回异常（HTTP {response.status_code}）')
    entries=[]
    for line in response.text.splitlines():
        value=line.strip()
        if not value or value.startswith('#'):continue
        entries.append(value if '://' in value else f'{scheme}://{value}')
        if len(entries)>=API_MAX_COUNT:break
    existing={item['url'] for item in (await get_proxies(db))['items']}
    unique=list(dict.fromkeys(entries))
    fresh=[value for value in unique if value not in existing]
    result=await append_proxies(db,'\n'.join(fresh),False,is_dynamic)
    result={'added':len(fresh),'skipped':len(unique)-len(fresh),'fetched':len(unique),'protocol':scheme,
            'items':result['items'],'total':result['total'],'enabled':result['enabled']}
    return result


async def delete_proxies(db,ids:list[int])->int:
    marks=','.join('?' for _ in ids)
    async with db.execute(f'DELETE FROM proxies WHERE id IN ({marks})',ids) as cur:deleted=cur.rowcount
    await db.commit();return deleted


def _client_for(url:str,timeout:float):
    scheme=url.split('://',1)[0].lower()
    if scheme in ('socks4','socks5','socks5h'):
        from httpx_socks import AsyncProxyTransport
        return httpx.AsyncClient(transport=AsyncProxyTransport.from_url(url),timeout=timeout,follow_redirects=False,trust_env=False)
    return httpx.AsyncClient(proxy=url,timeout=timeout,follow_redirects=False,trust_env=False)


def _probe_error(error:BaseException)->str:
    cause=error
    for _ in range(6):
        if isinstance(cause,ssl.SSLCertVerificationError):return 'TLS 证书校验失败，疑似 TLS 中间人代理'
        cause=cause.__cause__ or cause.__context__
        if cause is None:break
    names={klass.__name__ for klass in type(error).__mro__}
    if isinstance(error,(httpx.TimeoutException,TimeoutError)) or names&{'ProxyTimeoutError','ProxyEmptyResponseError'}:return '连接超时'
    if isinstance(error,httpx.ProxyError) or names&{'ProxyAuthenticationError','ProxyProtocolError','SOCKS5AuthError'}:return '代理握手失败，请检查协议或认证信息'
    if isinstance(error,httpx.ConnectError) or names&{'ProxyConnectionError'}:return '无法连接到代理'
    if isinstance(error,httpx.RemoteProtocolError) or names&{'ProxyError'}:return '代理转发异常'
    if isinstance(error,ssl.SSLError):return 'TLS 握手失败'
    if isinstance(error,OSError):return '网络错误，请检查地址或线路'
    return '测试失败，请稍后重试'


def _probe_failure(error:BaseException)->str:
    name=type(error).__name__
    if isinstance(error,ValueError) and 'scheme' in str(error).lower():
        return f'当前测试不支持该协议（{error}），请改用 socks5'
    if any(klass.__module__.startswith(('anyio','python_socks','httpx_socks')) for klass in type(error).__mro__):
        if 'Timeout' in name:return '连接超时'
        if 'Authentication' in name or 'Protocol' in name:return '代理握手失败，请检查协议或认证信息'
        if 'Connection' in name:return '无法连接到代理'
        if 'Broken' in name or 'EndOfStream' in name or 'Closed' in name:return '代理连接被中断'
        return '代理连接异常'
    return _probe_error(error)


async def _probe(url:str,timeout:float=PROBE_TIMEOUT)->dict:
    started=time.perf_counter()
    try:
        async with _client_for(url,timeout) as client:
            response=await client.get(PROBE_TARGET)
        latency=round((time.perf_counter()-started)*1000)
        if response.status_code>=500:return {'ok':False,'error':'探测目标暂时不可用'}
        exit_ip=None
        try:exit_ip=response.json().get('ip')
        except ValueError:exit_ip=response.text.strip()[:64] or None
        return {'ok':True,'latency_ms':latency,'exit_ip':exit_ip}
    except Exception as e:
        if isinstance(e,(asyncio.CancelledError,KeyboardInterrupt,SystemExit)):raise
        return {'ok':False,'latency_ms':round((time.perf_counter()-started)*1000),'error':_probe_failure(e)}


async def test_proxy(db,pid:int)->dict:
    async with db.execute('SELECT url,protocol,is_dynamic FROM proxies WHERE id=?',(pid,)) as cur:row=await cur.fetchone()
    if not row:raise ValueError('代理不存在')
    is_dynamic=bool(row['is_dynamic'])
    schemes=[row['protocol']]+(['http'] if row['protocol']=='https' else [])
    result={'ok':False,'protocol':row['protocol'],'tested_protocol':row['protocol'],'latency_ms':0,'exit_ips':[],'is_dynamic':is_dynamic}
    for index,scheme in enumerate(schemes):
        candidate=row['url'] if index==0 else re.sub(r'^https://',f'http://',row['url'])
        if is_dynamic:
            samples=await asyncio.gather(*(_probe(candidate) for _ in range(DYNAMIC_SAMPLES)))
        else:
            samples=[await _probe(candidate)]
        successes=[s for s in samples if s['ok']]
        exits=list(dict.fromkeys(s['exit_ip'] for s in successes if s['exit_ip']))
        latency=round(sum(s['latency_ms'] for s in successes)/len(successes)) if successes else samples[0]['latency_ms']
        result={**result,'ok':bool(successes),'latency_ms':latency,'exit_ips':exits,'exit_ip':exits[0] if exits else None,
                'tested_protocol':scheme}
        if not successes and samples[0].get('error'):result={**result,'error':samples[0]['error']}
        if successes or index==len(schemes)-1:break
    result['rotating']=is_dynamic and len(result['exit_ips'])>1
    return result


async def test_proxies(db,ids:list[int],concurrency:int=PROBE_CONCURRENCY)->list[dict]:
    unique=list(dict.fromkeys(ids))
    semaphore=asyncio.Semaphore(max(1,concurrency))
    async def run(pid):
        async with semaphore:
            try:return {'id':pid,**await test_proxy(db,pid)}
            except ValueError as e:return {'id':pid,'ok':False,'error':str(e)}
            except Exception:return {'id':pid,'ok':False,'error':'测试失败，请稍后重试'}
    return list(await asyncio.gather(*(run(pid) for pid in unique)))
