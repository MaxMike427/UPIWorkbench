"""解析器单测：覆盖标准 `---` 四段格式 + 标题行跳过。"""
from app.utils import parser


def test_parse_standard_four_fields():
    line = "a@dr.com---P@ss!123---ABCDEF123456---eyJhbGciOiJSUzI1NiJ9.payload.sig"
    r = parser.parse_line(line, 1)
    assert r is not None
    assert r.account == "a@dr.com"
    assert r.password == "P@ss!123"
    assert r.twofa_secret == "ABCDEF123456"
    assert r.access_token.startswith("eyJ")


def test_parse_skips_title_and_empty():
    content = "卡密导出\n\na@dr.com---pw---2FA---eyJabc\n# comment\n"
    records, skipped = parser.parse_txt(content)
    assert len(records) == 1
    assert skipped >= 1


def test_parse_missing_optional_fields():
    r = parser.parse_line("user@x.com---onlypass", 1)
    assert r is not None
    assert r.twofa_secret == "" and r.access_token == ""


def test_roundtrip_to_txt_row():
    row = parser.to_txt_row("a", "b", "c", "eyJx")
    assert row == "a---b---c---eyJx"
