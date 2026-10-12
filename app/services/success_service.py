"""成功库与仪表盘共用全流程成功口径，导出按任务或选中账号限定。"""
from __future__ import annotations

from ..utils.parser import to_success_txt_row

SUCCESS_FILTER = (
    "EXISTS (SELECT 1 FROM batch_members m JOIN task_batches b ON b.id=m.batch_id"
    " WHERE m.credential_id=c.id AND ((b.workflow_version=1 AND m.stage='upi_ok')"
    " OR (b.workflow_version=2 AND m.stage='done'"
    " AND EXISTS (SELECT 1 FROM sms_tasks s WHERE s.id=m.sms_task_id AND s.credential_id=c.id AND s.status='bound')"
    " AND EXISTS (SELECT 1 FROM upi_tasks p WHERE p.id=m.upi_task_id AND p.credential_id=c.id AND p.status='success'))))"
    " AND EXISTS (SELECT 1 FROM upi_tasks u WHERE u.credential_id=c.id"
    " AND u.status='success' AND COALESCE(u.upi_link,'')!='')"
)


async def count_successes(db) -> int:
    async with db.execute(f"SELECT COUNT(*) AS c FROM credentials c WHERE {SUCCESS_FILTER}") as cur:
        return (await cur.fetchone())["c"]


async def list_successes(db, keyword: str | None = None, page: int = 1, page_size: int = 50) -> dict:
    where = SUCCESS_FILTER
    args = []
    if keyword:
        where += " AND c.account LIKE ?"
        args.append(f"%{keyword}%")
    async with db.execute(f"SELECT COUNT(*) AS c FROM credentials c WHERE {where}", args) as cur:
        total = (await cur.fetchone())["c"]
    async with db.execute(
        "SELECT c.id, c.account, c.updated_at,"
        " (SELECT u.upi_link FROM upi_tasks u WHERE u.credential_id=c.id AND u.status='success'"
        " AND COALESCE(u.upi_link,'')!='' ORDER BY u.id DESC LIMIT 1) AS upi_link"
        f" FROM credentials c WHERE {where} ORDER BY c.id DESC LIMIT ? OFFSET ?",
        (*args, page_size, (page - 1) * page_size),
    ) as cur:
        items = [dict(r) async for r in cur]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def parse_selected_ids(value: str | None) -> list[int]:
    if not value or any(not item.strip().isdigit() for item in value.split(",")):
        raise ValueError("请选择有效的成功账号")
    ids = list(dict.fromkeys(int(item.strip()) for item in value.split(",")))
    if any(cid <= 0 for cid in ids) or len(ids) > 2000:
        raise ValueError("账号编号无效或超过一次导出上限（2000 个）")
    return ids


async def export_successes(db, ids: list[int] | None = None, batch_id: int | None = None) -> tuple[str, int]:
    if ids is None and batch_id is None:
        raise ValueError("请指定选中账号或任务")
    where = SUCCESS_FILTER
    args = []
    if ids is not None:
        if not ids:
            raise ValueError("请先选择成功账号")
        where += f" AND c.id IN ({','.join('?' for _ in ids)})"
        args.extend(ids)
    if batch_id is not None:
        where += " AND EXISTS (SELECT 1 FROM batch_members bm WHERE bm.batch_id=?"
        where += " AND bm.credential_id=c.id AND bm.stage IN ('done','upi_ok'))"
        args.append(batch_id)
    async with db.execute(
        f"SELECT c.id,c.account,c.password,COALESCE(c.twofa_secret,'') AS twofa FROM credentials c"
        f" WHERE {where} ORDER BY c.id", args,
    ) as cur:
        rows = [dict(r) async for r in cur]
    if ids is not None and len(rows) != len(ids):
        raise ValueError("部分选中账号不存在或未完成全流程")
    if not rows:
        raise ValueError("当前任务暂无可导出的成功账号")
    return "\n".join(to_success_txt_row(r["account"], r["password"], r["twofa"]) for r in rows), len(rows)
