"""txt 解析器。

标准行格式（见 卡密导出_20261012.txt）：
    账号---密码---2FA密钥---AccessToken(AT, ey 开头)

兼容：
- 分隔符 `---`（标准）；退化支持 `----` / 制表符 / `,` / 空格+AT 场景
- 首行标题（如 `卡密导出`）、空行、`#` 注释行自动跳过
- 字段缺失时按位置补齐：account/password 必填，twofa/at 可空
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SEPARATOR = "---"


@dataclass
class ParsedCredential:
    account: str
    password: str
    twofa_secret: str = ""
    access_token: str = ""
    line_no: int = 0
    raw: str = ""


def _split_line(line: str) -> list[str]:
    if SEPARATOR in line:
        # 密码本身可能含 `-`，但标准分隔是三个连字符；用正则按 --- 切分
        # 避免 `----` 被切出空字段：先把 4+ 连字符归一为 ---
        normalized = re.sub(r"-{4,}", SEPARATOR, line)
        return [p.strip() for p in normalized.split(SEPARATOR)]
    for sep in ["\t", ","]:
        if sep in line:
            return [p.strip() for p in line.split(sep)]
    # 空格分隔兜底：最后一个 token 若 ey 开头则为 AT
    parts = line.split()
    if len(parts) >= 3:
        return parts
    return [line.strip()]


def parse_line(line: str, line_no: int = 0) -> ParsedCredential | None:
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    # 跳过纯标题行（无分隔符、无空格、无 ey token）
    if SEPARATOR not in s and "\t" not in s and "," not in s and " " not in s:
        return None
    parts = _split_line(s)
    # 过滤因 `----` 产生的空字段
    parts = [p for p in parts if p != ""]
    if not parts:
        return None
    account = parts[0] if len(parts) > 0 else ""
    password = parts[1] if len(parts) > 1 else ""
    twofa = ""
    at = ""
    rest = parts[2:]
    for p in rest:
        if p.startswith("eyJ") and not at:
            at = p
        elif not twofa:
            twofa = p
        elif not at:
            at = p
    if not account or not password:
        return None
    return ParsedCredential(
        account=account, password=password, twofa_secret=twofa,
        access_token=at, line_no=line_no, raw=s,
    )


def parse_txt(content: str) -> tuple[list[ParsedCredential], int]:
    """解析全文。返回 (成功记录, 跳过行数)。"""
    records: list[ParsedCredential] = []
    skipped = 0
    for i, line in enumerate(content.splitlines(), start=1):
        r = parse_line(line, line_no=i)
        if r is None:
            if line.strip():
                skipped += 1
            continue
        records.append(r)
    return records, skipped


def to_txt_row(account: str, password: str, twofa: str = "", at: str = "") -> str:
    return SEPARATOR.join([account, password, twofa or "", at or ""])


def to_success_txt_row(account: str, password: str, twofa: str = "") -> str:
    """成功库导出格式：账号----密码----2FA，不导出访问令牌。"""
    return "----".join([account, password, twofa or ""])
