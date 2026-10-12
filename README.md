# UPI Workbench

账号任务工作台：导入 TXT 账号 → 资格检查 → ytc 提链支付 → 接码绑定 → 成功库导出。

本目录是可发布的脱敏源码副本，不包含运行数据库、账号 TXT、任务报告、JSON 产物、日志、虚拟环境或真实 CDK/API Key。

## 工作区

前端严格保留五个模块：

1. 仪表盘：账号总数、全流程成功数、实时终端日志。
2. 账号库：TXT 导入、购买链接、账号选择、分页、删除。
3. 任务管理：创建、执行、暂停/继续、详情、失败重试、TXT/MD 导出。
4. 成功库：全流程成功账号、TXT 导出、CPA JSON/sub2api JSON/双格式 ZIP 下载。
5. 设置：管理员密码、接码 CDK、提链支付 CDK、并发控制。

## Quick Start

要求 Python 3.11+。

```bash
cd UPIWorkbench
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python run.py
```

默认监听 `127.0.0.1:8000`。首次启动会创建配置中的管理员账号；生产环境启动后请修改密码和 JWT 密钥。

## 配置

`config.yaml` 不纳入版本库，真实密钥只通过本地配置或环境变量提供。重要环境变量：

| 变量 | 说明 |
|---|---|
| `UPI_DB_PATH` | SQLite 路径 |
| `UPI_JWT_SECRET` | JWT 密钥，生产环境使用随机长密钥 |
| `UPI_ADMIN_USER` / `UPI_ADMIN_PASS` | 首次管理员账号/密码 |
| `UPI_DATA_DIR` | CPA/sub2api 文件保存目录 |
| `UPI_REPORT_DIR` | 任务 Markdown 记录目录 |
| `UPI_PURCHASE_URL` | 兼容配置；当前购买入口使用 `static/index.html` 中的明确链接 |

设置页中的两个 CDK 不会回显。ytc 的 `ohk_...` 支付 CDK 用于 ytc 账户认证；接码 CDK 仅用于 handy。

## 执行规则

- TXT 导入的原始 AT 会保存为任务快照。
- `token_ok=true` 且 `eligible=true` 才能进入提链；查询异常或 HTTP 403 不等同于明确无资格。
- ytc 提链最多 5 次；支付结果未确认时继续查询原订单，不重复提交。
- 支付确认成功后接码不限尝试次数，失败按间隔重试；暂停任务不会丢失外部订单。
- 接码完成后自动下载 CPA 与 sub2api JSON 到 `data/account_exports/`。
- 每个任务结束写入 `doc/task_reports/task-<id>-YYYYMMDD.md`；报告不写入密码、2FA、AT、CDK 或支付链接。

## 测试

```bash
pytest tests/ -q
node --test tests/ui_workflow.test.cjs
```

浏览器验收使用隔离 SQLite 和第三方服务替身，不会消耗真实支付/接码额度：

```bash
python tests/browser_acceptance.py
```

## 目录说明

- `app/`：FastAPI、SQLite、认证、编排与第三方适配器。
- `static/index.html`：五模块 Alpine.js/Tailwind CDN 单页界面。
- `tests/`：后端单元/集成测试、UI 行为测试、隔离浏览器验收。
- `doc/20261012.md`：最新产品要求。
- `doc/IMPLEMENTATION_20261012.md`：实现、流程、API 与验收记录。
- `INTEGRATION.md`：第三方接口实测契约。

## 安全提醒

发布前检查：

1. 不要把 `config.yaml`、SQLite、账号 TXT、任务报告或 `data/account_exports/` 加入 Git。
2. 使用 HTTPS、强管理员密码和随机 JWT 密钥。
3. 先在设置页配置 CDK，再执行真实任务。
4. 真实第三方支付和接码流程可能受账号、额度、线路及平台风控影响。

## Version

Current source release: `v1.0.0`.
