"""代理库：校验 URI，存储真实凭证但只向页面返回脱敏地址。"""
from __future__ import annotations

import asyncio
import re
import ssl
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

FREE_ENDPOINT='https://proxy.scdn.io/api/get_proxy.php'
PROBE_TARGET='https://api.ipify.org?format=json'
PROBE_TIMEOUT=6.0
PROBE_CONCURRENCY=8


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
        items=[{'id':r['id'],'url':masked_proxy(r['url']),'protocol':r['protocol'],'host':r['host'],'port':r['port']} async for r in cur]
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


async def append_proxies(db,text:str,enabled:bool)->dict:
    async with db.execute('SELECT url FROM proxies') as cur:existing={r['url'] async for r in cur}
    parsed=[];seen=set(existing)
    for i,line in enumerate(text.splitlines(),1):
        if not line.strip():continue
        try:item=parse_proxy(line)
        except ValueError as e:raise ValueError(f'第 {i} 行：{e}')
        if item['url'] not in seen:parsed.append(item);seen.add(item['url'])
    if enabled and not (existing or parsed):raise ValueError('代理库开启时至少需要一个有效代理')
    for item in parsed:
        await db.execute('INSERT INTO proxies(url,protocol,host,port) VALUES(?,?,?,?)',tuple(item[k] for k in ('url','protocol','host','port')))
    await db.execute("UPDATE configs SET value=?,updated_at=CURRENT_TIMESTAMP WHERE key='proxy.enabled'",('true' if enabled else 'false',))
    await db.commit()
    return await get_proxies(db)


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
    except (httpx.HTTPError,ssl.SSLError,OSError,ValueError) as e:
        return {'ok':False,'latency_ms':round((time.perf_counter()-started)*1000),'error':_probe_error(e)}


async def test_proxy(db,pid:int)->dict:
    async with db.execute('SELECT url,protocol FROM proxies WHERE id=?',(pid,)) as cur:row=await cur.fetchone()
    if not row:raise ValueError('代理不存在')
    schemes=[row['protocol']]+(['http'] if row['protocol']=='https' else [])
    result={'ok':False,'protocol':row['protocol'],'tested_protocol':row['protocol'],'latency_ms':0}
    for index,scheme in enumerate(schemes):
        candidate=row['url'] if index==0 else re.sub(r'^https://',f'http://',row['url'])
        outcome=await _probe(candidate)
        result={**result,**outcome,'tested_protocol':scheme}
        if outcome['ok'] or index==len(schemes)-1:break
    return result


async def test_proxies(db,ids:list[int],concurrency:int=PROBE_CONCURRENCY)->list[dict]:
    unique=list(dict.fromkeys(ids))
    semaphore=asyncio.Semaphore(max(1,concurrency))
    async def run(pid):
        async with semaphore:
            try:return {'id':pid,**await test_proxy(db,pid)}
            except ValueError as e:return {'id':pid,'ok':False,'error':str(e)}
    return list(await asyncio.gather(*(run(pid) for pid in unique)))


async def import_free_proxies(db,protocol:str,country_code:str,count:int,enabled:bool)->dict:
    params={'protocol':protocol,'country_code':'all' if country_code.lower()=='all' else country_code.upper(),'count':count}
    try:
        async with httpx.AsyncClient(timeout=15.0,follow_redirects=False,trust_env=False) as client:
            response=await client.get(FREE_ENDPOINT,params=params)
            data=response.json()
    except (httpx.HTTPError,ValueError):raise ValueError('免费代理服务暂时不可用')
    if response.status_code>=400 or not isinstance(data,dict) or data.get('code') not in (200,0):
        raise ValueError('免费代理服务返回异常')
    values=((data.get('data') or {}).get('proxies') or [])
    def as_proxy(value):
        if isinstance(value,dict):
            raw=value.get('proxy') or value.get('address') or value.get('url')
            if not raw and value.get('ip') and value.get('port'):raw=f"{value['ip']}:{value['port']}"
            detected=str(value.get('protocol') or value.get('type') or '').lower()
        elif isinstance(value,str):
            raw=value;detected=''
        else:return ''
        if not raw:return ''
        if '://' in raw:return raw
        scheme=detected if detected in ('http','https','socks4','socks5') else ('http' if protocol=='all' else protocol)
        return f'{scheme}://{raw}'
    text='\n'.join(item for value in values if (item:=as_proxy(value)))
    return await append_proxies(db,text,enabled)
