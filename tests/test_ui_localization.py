from pathlib import Path


UI = Path(__file__).parents[1] / "static" / "index.html"


def test_ui_defines_chinese_labels_for_backend_enums():
    html = UI.read_text(encoding="utf-8")

    assert "statusText(value)" in html
    assert "stepText(value)" in html
    assert "configLabel(key)" in html
    assert "pending:'待处理'" in html
    assert "needs_review:'待人工处理'" in html
    assert "qualification:'资格检查'" in html
    assert "'sms_provider.cdk':'接码 CDK'" in html
    assert "'upi_provider.cdk':'提链支付 CDK'" in html


def test_ui_renders_localized_values_instead_of_raw_enums():
    html = UI.read_text(encoding="utf-8")

    assert "x-text=\"statusText(b.status)\"" in html
    assert "x-text=\"statusText(c.status)\"" in html
    assert "x-text=\"stepText(b.current_step)\"" in html
    assert "x-text=\"stepText(m.stage)\"" in html
    assert "x-text=\"configLabel(c.key)\"" in html


def test_masked_config_requires_a_new_value_before_saving():
    html = UI.read_text(encoding="utf-8")

    assert "c.masked&&(!c.value||c.value==='****')" in html
    assert "已脱敏配置未修改" in html
