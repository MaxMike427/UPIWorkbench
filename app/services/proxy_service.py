"""代理库：校验 URI，存储真实凭证但只向页面返回脱敏地址。"""
from __future__ import annotations

import time
from urllib.parse import urlsplit, urlunsplit

import httpx


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


async def test_proxy(db,pid:int)->dict:
    async with db.execute('SELECT url,protocol,host,port FROM proxies WHERE id=?',(pid,)) as cur:row=await cur.fetchone()
    if not row:raise ValueError('代理不存在')
    started=time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=15.0,follow_redirects=False,trust_env=False) as client:
            response=await client.post('https://proxy.scdn.io/api/test_proxy.php',data={
                'ip':row['host'],'port':row['port'],'protocols':row['protocol'].replace('socks5h','socks5').upper()})
            data=response.json()
        if response.status_code>=400 or not isinstance(data,dict):
            raise ValueError('proxy.scdn.io 测试接口返回异常')
        ok=data.get('status')=='success'
        return {'ok':ok,'status':data.get('status','error'),'results':data.get('results',{}),
                'latency_ms':round((time.perf_counter()-started)*1000),'protocol':row['protocol']}
    except (httpx.HTTPError,ValueError,OSError,ImportError):
        return {'ok':False,'latency_ms':round((time.perf_counter()-started)*1000),'protocol':row['protocol'],'error':'代理连接失败，请检查地址或线路'}


async def test_proxies(db,ids:list[int])->list[dict]:
    results=[]
    for pid in ids:
        try:
            result=await test_proxy(db,pid)
            result['id']=pid
        except ValueError as e:
            result={'id':pid,'ok':False,'error':str(e)}
        results.append(result)
    return results


async def import_free_proxies(db,protocol:str,country_code:str,count:int,enabled:bool)->dict:
    params={'protocol':protocol,'country_code':'all' if country_code.lower()=='all' else country_code.upper(),'count':count}
    try:
        async with httpx.AsyncClient(timeout=15.0,follow_redirects=False,trust_env=False) as client:
            response=await client.get('https://proxy.scdn.io/api/get_proxy.php',params=params)
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
