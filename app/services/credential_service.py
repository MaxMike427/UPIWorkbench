"""凭证导入/查询/导出/删除（M1 基础 CRUD；M2 补 Web UI 预览）。"""
from __future__ import annotations

from ..database import log_action
from ..models import ImportResult
from ..utils import parser
from ..utils.helpers import utcnow_iso


async def import_from_txt(db, content: str, filename: str, admin_id: int | None = None, ip: str = "") -> ImportResult:
    records, skipped = parser.parse_txt(content)
    imported = 0
    for r in records:
        await db.execute(
            "INSERT INTO credentials(account, password, twofa_secret, access_token, original_access_token, source_file, status)"
            " VALUES(?,?,?,?,?,?, 'pending')",
            (r.account, r.password, r.twofa_secret, r.access_token, r.access_token, filename),
        )
        imported += 1
    await db.commit()
    await log_action(db, admin_id, "import", "credentials", None,
                     f"{filename}: imported={imported} skipped={skipped}", ip)
    return ImportResult(imported=imported, skipped=skipped, filename=filename)


async def list_credentials(db, status: str | None = None, keyword: str | None = None,
                           page: int = 1, page_size: int = 20,
                           source_file: str | None = None) -> tuple[list[dict], int]:
    conds, args = [], []
    if status:
        conds.append("status=?")
        args.append(status)
    if keyword:
        conds.append("account LIKE ?")
        args.append(f"%{keyword}%")
    if source_file is not None:
        conds.append("COALESCE(source_file,'')=?")
        args.append(source_file)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    async with db.execute(f"SELECT COUNT(*) AS c FROM credentials {where}", args) as cur:
        total = (await cur.fetchone())["c"]
    offset = (page - 1) * page_size
    async with db.execute(
        f"SELECT id, account, password, COALESCE(twofa_secret,'') AS twofa_secret,"
        f" COALESCE(access_token,'') AS access_token, COALESCE(source_file,'') AS source_file,"
        f" status, COALESCE(error_msg,'') AS error_msg FROM credentials {where}"
        f" ORDER BY id DESC LIMIT ? OFFSET ?",
        (*args, page_size, offset),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows], total


async def list_sources(db) -> list[dict]:
    async with db.execute("SELECT COALESCE(source_file,'') AS source_file,COUNT(*) AS total"
                          " FROM credentials GROUP BY COALESCE(source_file,'') ORDER BY source_file") as cur:
        return [dict(r) async for r in cur]


async def export_credentials(db, status: str | None = None) -> str:
    args: list = []
    where = ""
    if status:
        where = "WHERE status=?"
        args.append(status)
    async with db.execute(
        f"SELECT account, password, COALESCE(twofa_secret,'') AS twofa_secret,"
        f" COALESCE(access_token,'') AS access_token FROM credentials {where} ORDER BY id",
        args,
    ) as cur:
        rows = await cur.fetchall()
    return "\n".join(parser.to_txt_row(r["account"], r["password"], r["twofa_secret"], r["access_token"]) for r in rows)


async def delete_credential(db, cid: int) -> bool:
    await _guard_in_use(db,[cid])
    async with db.execute("DELETE FROM credentials WHERE id=?", (cid,)) as cur:
        ok = cur.rowcount > 0
    await db.commit()
    return ok


async def batch_delete(db, ids: list[int]) -> int:
    ids=list(dict.fromkeys(ids))
    await _guard_in_use(db,ids)
    q = ",".join("?" for _ in ids)
    async with db.execute(f"DELETE FROM credentials WHERE id IN ({q})", ids) as cur:
        n = cur.rowcount
    await db.commit()
    return n


async def _guard_in_use(db,ids:list[int]) -> None:
    q=','.join('?' for _ in ids)
    async with db.execute(
        f"SELECT m.credential_id FROM batch_members m JOIN task_batches b ON b.id=m.batch_id"
        f" WHERE m.credential_id IN ({q}) AND b.deleted_at IS NULL"
        " AND b.status IN ('created','running','paused') LIMIT 1",ids) as cur:
        row=await cur.fetchone()
    if row:raise ValueError('部分选中账号正在被待执行、执行中或暂停任务使用，请先处理相关任务')


async def update_status(db, cid: int, status: str, error: str | None = None) -> None:
    await db.execute(
        "UPDATE credentials SET status=?, error_msg=?, updated_at=? WHERE id=?",
        (status, error or "", utcnow_iso(), cid),
    )
    await db.commit()
