"""本地 GitHub 提链错误摘要与线上仓库同步边界。"""
from __future__ import annotations

from types import SimpleNamespace


def test_local_checkout_http_error_keeps_safe_upstream_code(monkeypatch):
    from app.upi_zero_link import protocol

    class Response:
        status_code = 400
        text = '{"detail":"invalid checkout session"}'
        def json(self): return {"detail": "invalid checkout session"}

    monkeypatch.setattr(protocol, "chatgpt_session", lambda *args, **kwargs: SimpleNamespace(get=lambda *args, **kwargs: None))
    monkeypatch.setattr(protocol.oaics, "warmup_chatgpt_page", lambda *args, **kwargs: None)
    monkeypatch.setattr(protocol, "checkout_risk_headers", lambda *args, **kwargs: {})
    monkeypatch.setattr(protocol, "request_http", lambda *args, **kwargs: Response())
    result = protocol.extract({"email":"a@example.com","access_token":"eyJtest"}, "http://proxy.example:80", emit=lambda *_: None)
    assert result == {"ok": False, "error": "checkout_http_400", "upstream_code": "invalid checkout session"}


def test_local_checkout_error_is_visible_as_a_safe_runtime_summary():
    from app.services.report_service import safe_error
    assert safe_error("checkout_http_400 (invalid checkout session)") == "本地提链结账被上游拒绝（HTTP 400: invalid checkout session）"
