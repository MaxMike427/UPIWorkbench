"""在 Node 中实际执行前端方法，避免只检查文本就宣称流程完成。"""
from pathlib import Path
import subprocess


def test_five_module_ui_workflows():
    test_file = Path(__file__).with_name("ui_workflow.test.cjs")
    result = subprocess.run(["node", "--test", str(test_file)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
