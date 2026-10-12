"""测试写入的报告和账号 JSON 都使用临时目录，不污染正式 data/doc。"""
import pytest


@pytest.fixture(autouse=True)
def isolated_artifact_directories(monkeypatch,tmp_path):
    from app.config import get_settings
    monkeypatch.setenv('UPI_DATA_DIR',str(tmp_path/'data'))
    monkeypatch.setenv('UPI_REPORT_DIR',str(tmp_path/'reports'))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
