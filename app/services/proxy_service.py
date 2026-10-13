"""代理库：校验 URI，存储真实凭证但只向页面返回脱敏地址。"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def parse_proxy(value:str) -> dict:
    raw=value.strip()
    if raw.startswith('sk5://'):raw='socks5://'+raw[6:]
    if any(ord(char)<33 for char in raw):raise ValueError('代理地址包含无效字符')
    try:
        url=urlsplit(raw)
        if url.scheme not in ('http','https','socks5','socks5h') or not url.hostname or not url.port:
            raise ValueError('格式无效')
        if url.path not in ('','/') or url.query or url.fragment:
            raise ValueError('不支持路径或参数')
        if url.username is not None and url.password is None:raise ValueError('认证信息不完整')
        port=url.port
    except ValueError:
        raise ValueError('请填写有效的 HTTP/HTTPS/SOCKS5 代理 URI 和端口')
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
