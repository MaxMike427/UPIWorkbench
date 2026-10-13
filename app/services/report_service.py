"""任务结束后生成非敏感 Markdown 记录，路径只由任务编号和日期决定。"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime

from ..config import get_settings


def safe_error(value: str) -> str:
    """报告/终端只写已知安全的摘要，不复制第三方原始 JSON。"""
    labels = {
        "not-eligible": "账号无资格", "empty-token": "缺少访问令牌", "jwt-expired": "访问令牌过期",
        "token-401": "访问令牌失效", "no_link": "未获取到支付链接", "no-next-action": "未获取到支付链接",
        "SentinelFlowUnavailableError": "接码平台登录校验失败", "timeout": "等待平台结果超时",
        "fetch-error": "平台网络请求失败", "quota": "平台额度不足",
        "代理": "代理连接异常，请检查代理库", "proxy": "代理连接异常，请检查代理库",
    }
    for needle, text in labels.items():
        if needle in str(value):
            return text
    return "平台操作异常，请在任务详情中核对" if value else "—"


async def write_task_report(db,batch_id:int) -> str:
    async with db.execute("SELECT * FROM task_batches WHERE id=?",(batch_id,)) as cur:
        batch=await cur.fetchone()
    async with db.execute(
        "SELECT c.account,m.stage,m.error FROM batch_members m LEFT JOIN credentials c ON c.id=m.credential_id"
        " WHERE m.batch_id=? ORDER BY m.credential_id",(batch_id,)) as cur:
        members=[dict(r) async for r in cur]
    success=sum(m["stage"] in ("done","upi_ok") for m in members)
    no_sms=sum(m['stage']=='paid_without_sms' for m in members)
    labels={"done":"全流程成功","upi_ok":"历史流程成功","qual_failed":"资格检查失败", "upi_failed":"提链支付失败",
            "paid":"支付成功，待接码","paid_without_sms":"已成功未接码","bind_failed":"接码失败","qualified":"资格通过","pending":"未执行"}
    snapshot=json.loads(batch['snapshot_json'] or '{}') if batch['snapshot_json'] else {}
    flow='TXT 原 AT → 资格检查 → ytc 提链支付 → 已成功未接码 → 成功库' if snapshot.get('sms_enabled') is False else 'TXT 原 AT → 资格检查 → ytc 提链支付 → 自动接码绑定 → 成功库'
    lines=[f"# UPI 任务 #{batch_id} 执行记录", "",
           f"- 记录时间：{datetime.now().isoformat(timespec='seconds')}",
           f"- 任务状态：{'成功' if batch['status']=='completed' else '失败'}",
           f"- 流程：{flow}",
           f"- 总账号：{len(members)}；全流程成功：{success}；已成功未接码：{no_sms}；未成功：{len(members)-success-no_sms}",
           f"- 来源任务：{batch['source_batch_id'] or '原始任务'}", "",
           "| 邮箱账号 | 执行结果 | 说明 |","|---|---|---|"]
    for m in members:
        account=(m["account"] or "已移除账号").replace('|','\\|').replace('\n',' ')
        lines.append(f"| {account} | {labels.get(m['stage'],'执行异常')} | {safe_error(m['error'])} |")
    lines.extend(["","> 此记录不包含账号密码、2FA 密钥、访问令牌、验证码、CDK 或支付链接。",""])
    directory=get_settings().report_dir
    directory.mkdir(parents=True,exist_ok=True)
    target=directory / f"task-{batch_id}-{datetime.now().strftime('%Y%m%d')}.md"
    def save():
        temp=target.with_suffix('.md.tmp')
        temp.write_text('\n'.join(lines),encoding='utf-8')
        temp.chmod(0o600)
        temp.replace(target)
    await asyncio.to_thread(save)
    await db.execute("UPDATE task_batches SET report_path=? WHERE id=?",(str(target),batch_id))
    await db.commit()
    return str(target)
