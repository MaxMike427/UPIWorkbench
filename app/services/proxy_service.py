"""代理库：校验 URI，存储真实凭证但只向页面返回脱敏地址。"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import ssl
import time
import weakref
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

PROBE_TARGET='https://api.ipify.org?format=json'
PROBE_TIMEOUT=6.0
PROBE_CONCURRENCY=8
DYNAMIC_SAMPLES=3
_DYNAMIC_RATE = weakref.WeakKeyDictionary()


_COLON_CREDENTIAL=re.compile(r'^(?P<scheme>[a-z0-9+.-]*://)?(?P<host>[^:\[\]/@]+):(?P<port>\d{1,5}):(?P<user>[^:@/\s]+):(?P<pass>[^:@/\s]+)$',re.I)


def _from_colon_form(raw:str)->str:
    """把代理商常见的 host:port:user:pass 文本改写成带协议的 URI。"""
    match=_COLON_CREDENTIAL.match(raw.strip())
    if not match:return raw
    scheme=(match.group('scheme') or 'http://').lower()
    user=quote(match.group('user'),safe='')
    password=quote(match.group('pass'),safe='')
    return f'{scheme}{user}:{password}@{match.group("host")}:{match.group("port")}'


def parse_proxy(value:str) -> dict:
    raw=value.strip()
    if raw.startswith('sk5://'):raw='socks5://'+raw[6:]
    raw=_from_colon_form(raw)
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


def session_variant(url:str,session_id:str)->str:
    """为账号分配独立的粘性会话，网关在会话期内固定同一出口 IP。"""
    parts=urlsplit(url)
    if parts.username is None or parts.password is None:return url
    user=parts.username
    if '-session-' in user:return url
    host=f'[{parts.hostname}]' if ':' in (parts.hostname or '') else parts.hostname
    return f'{parts.scheme}://{quote(user+"-session-"+session_id,safe="")}:{quote(parts.password,safe="")}@{host}:{parts.port}'


def masked_proxy(value:str)->str:
    url=urlsplit(value)
    host=f'[{url.hostname}]' if ':' in (url.hostname or '') else url.hostname
    auth='****:****@' if url.username is not None else ''
    return f'{url.scheme}://{auth}{host}:{url.port}'


def validate_dynamic_url(value:str)->str:
    try:
        url=urlsplit(value.strip())
        host=url.hostname or ''
        official_host=(host=='711proxy.com' or host.endswith('.711proxy.com') or
                       host.endswith('.rotgbapi.trustsource.live'))
        allowed_port=url.port in (None,80,443) or (host.endswith('.rotgbapi.trustsource.live') and url.port==8089)
        if (url.scheme not in ('http','https') or not official_host
                or url.username is not None or url.password is not None or url.fragment
                or not allowed_port or any(ord(c)<33 for c in value)):
            raise ValueError()
    except ValueError:
        raise ValueError('请填写 711proxy 官方控制台生成的 HTTP/HTTPS API URL，不支持其它域名或内网地址')
    return value.strip()


async def dynamic_config_values(db)->dict:
    async with db.execute("SELECT key,value FROM configs WHERE key LIKE 'proxy.dynamic.%'") as cur:
        values={r['key']:r['value'] async for r in cur}
    return {'api_url':values.get('proxy.dynamic.api_url',''),
            'protocol':values.get('proxy.dynamic.protocol','http'),
            'enabled':values.get('proxy.dynamic.enabled')=='true'}


async def get_dynamic_config(db)->dict:
    config=await dynamic_config_values(db)
    return {'provider':'711proxy','protocol':config['protocol'],'enabled':config['enabled'],
            'url_saved':bool(config['api_url']), 'host':urlsplit(config['api_url']).hostname if config['api_url'] else '',
            'storage_key':'proxy.dynamic.api_url'}


async def save_dynamic_config(db,body)->dict:
    current=await dynamic_config_values(db)
    url=validate_dynamic_url(body.api_url) if body.api_url.strip() else current['api_url']
    if body.enabled and not url:raise ValueError('启用动态代理前请保存 711proxy API URL')
    for key,value in {'api_url':url,'protocol':body.protocol,'enabled':'true' if body.enabled else 'false'}.items():
        await db.execute("INSERT INTO configs(key,value) VALUES(?,?) ON CONFLICT(key)"
                         " DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",('proxy.dynamic.'+key,value))
    await db.commit()
    return await get_dynamic_config(db)


async def _validate_dynamic_host(host:str)->None:
    try:
        records=await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host,None,type=socket.SOCK_STREAM),timeout=10)
    except (OSError,TimeoutError):raise ValueError('711proxy 域名解析失败，请稍后重试') from None
    if not records or any(not ipaddress.ip_address(r[4][0]).is_global for r in records):
        raise ValueError('711proxy API 必须解析到公网地址')


def _dynamic_client():
    return httpx.AsyncClient(timeout=20,follow_redirects=False,trust_env=False)


def _dynamic_proxy_items(text:str,protocol:str)->list[dict]:
    raw=text.lstrip('\ufeff').strip()
    if raw.startswith(('{','[')):
        try:data=json.loads(raw)
        except ValueError:raise ValueError('711proxy 返回的 JSON 格式无效') from None
        if isinstance(data,dict):
            if data.get('success') is False or data.get('code') not in (None,0,200,'0','200'):
                raise ValueError('711proxy API 返回失败，请检查额度、提取链接和 IP 白名单')
            data=data.get('data',data.get('proxies',[]))
            if isinstance(data,dict):data=data.get('list',data.get('proxies',[]))
        if not isinstance(data,list):raise ValueError('711proxy 未返回代理列表，请选择 TXT 换行或 JSON 格式')
        lines=[]
        for row in data:
            if isinstance(row,str):lines.append(row)
            elif isinstance(row,dict):
                host=row.get('ip') or row.get('host') or row.get('server')
                port=row.get('port')
                if not host or not port:raise ValueError('711proxy 代理列表缺少主机或端口')
                auth=''
                user=row.get('username') or row.get('user')
                password=row.get('password')
                if user is not None and password is not None:auth=f'{quote(str(user),safe="")}:{quote(str(password),safe="")}@'
                lines.append(f'{protocol}://{auth}{host}:{port}')
            else:raise ValueError('711proxy 代理列表格式无效')
    else:
        lines=re.split(r'[\r\n\t]+',raw)
    if len(lines)>900:raise ValueError('单次最多提取 900 个代理')
    items=[];seen=set()
    for line in lines:
        if not line.strip():continue
        try:
            item=parse_proxy(line if '://' in line else protocol+'://'+line)
        except ValueError:raise ValueError('711proxy 返回内容不是有效代理，请使用 TXT 每行一个 host:port 或 JSON 格式') from None
        if item['url'] not in seen:items.append(item);seen.add(item['url'])
    if not items:raise ValueError('711proxy 未返回可用代理，请检查额度和 IP 白名单')
    return items


async def fetch_dynamic_proxies(config:dict)->list[dict]:
    url=validate_dynamic_url(config.get('api_url',''))
    await _validate_dynamic_host(urlsplit(url).hostname)
    # 711proxy 文档要求提取请求间隔至少 1 秒；不同任务和手动取链共用节流。
    loop=asyncio.get_running_loop()
    state=_DYNAMIC_RATE.get(loop)
    if state is None:
        state={'lock':asyncio.Lock(),'last':None};_DYNAMIC_RATE[loop]=state
    async with state['lock']:
        if state['last'] is not None:
            delay=1.0-(loop.time()-state['last'])
            if delay>0:await asyncio.sleep(delay)
        state['last']=loop.time()
    try:
        async with _dynamic_client() as client:
            async with client.stream('GET',url) as response:
                if response.status_code!=200:raise ValueError(f'711proxy API 请求失败（HTTP {response.status_code}），请检查提取链接和 IP 白名单')
                content=bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content)>200000:raise ValueError('711proxy API 响应过大，已停止读取')
    except (httpx.HTTPError,OSError):raise ValueError('711proxy API 连接失败，请检查网络或稍后重试') from None
    try:text=content.decode('utf-8-sig')
    except UnicodeDecodeError:raise ValueError('711proxy API 返回内容编码无效') from None
    return _dynamic_proxy_items(text,config.get('protocol','http'))


async def fetch_and_append_dynamic(db)->dict:
    config=await dynamic_config_values(db)
    items=await fetch_dynamic_proxies(config)
    async with db.execute("SELECT value FROM configs WHERE key='proxy.enabled'") as cur:row=await cur.fetchone()
    result=await append_proxies(db,'\n'.join(p['url'] for p in items),bool(row) and row['value']=='true',True)
    return {**result,'fetched':len(items)}


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
