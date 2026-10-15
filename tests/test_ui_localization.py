from pathlib import Path


UI = Path(__file__).parents[1] / "static" / "index.html"


def test_ui_defines_chinese_labels_for_backend_enums():
    html = UI.read_text(encoding="utf-8")

    assert "statusText(value)" in html
    assert "stepText(value)" in html
    assert "supplierDraftSummary()" in html
    assert "pending:'待处理'" in html
    assert "needs_review:'待人工处理'" in html
    assert "qualification:'资格检查'" in html
    assert "供应商配置" in html
    assert "ytc API Key（ohk_）" in html


def test_ui_renders_localized_values_instead_of_raw_enums():
    html = UI.read_text(encoding="utf-8")

    assert "x-text=\"statusText(b.status)\"" in html
    assert "x-text=\"statusText(c.status)\"" in html
    assert "x-text=\"stepText(b.current_step)\"" in html
    assert "x-text=\"stepText(m.stage)\"" in html
    assert "x-text=\"c.label\"" in html


def test_supplier_secret_fields_never_use_masked_values_as_input():
    html = UI.read_text(encoding="utf-8")

    assert "value.includes('****')" in html
    assert "不可保存脱敏占位符" in html
    assert "已保存，留空保持此供应商原值" in html
