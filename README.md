# UPI Workbench

账号任务工作台：导入 TXT 账号 → 资格检查 → 提炼 → 支付 → 接码绑定（可关闭）→ 成功库导出，并可通过外部代理库和 sub2api 集成扩展流程。

本项目使用 FastAPI、SQLite 和原生 Alpine.js 页面。发布源码不包含运行数据库、账号 TXT、任务报告、JSON 产物、日志、虚拟环境或真实 CDK/API Key。

## 工作区

V1.2.0 共七个模块：

1. **仪表盘**：账号统计、全流程成功/已成功未接码统计、实时终端日志。
2. **账号库**：TXT 导入、搜索、分页、跨页选择、精确创建任务和账号删除。
3. **代理库**：添加、导入、测试和批量删除代理；支持 HTTP/HTTPS/SOCKS4/SOCKS5 与带认证代理，支持 `host:port:用户:密码` 文本，无协议 `host:port` 默认按 HTTP；支持动态 IP（网关轮换）标记与多出口采样，网关代理按账号分配独立粘性会话；本地提炼可使用 HTTP/HTTPS/SOCKS5/SOCKS5H，远程 ytc 保持 HTTP/HTTPS/SOCKS5 限制。
4. **任务管理**：资格检查、独立提炼/支付服务商、可选接码、按任务自动推送开关、暂停/继续、详情、失败重试和 MD/TXT 导出。
5. **成功库**：区分全流程成功与已成功未接码；后者仅可导出 TXT，不生成伪造 JSON。
6. **集成**：配置 sub2api 推送模板、读取分组、查看队列、逐条重试或只读核对。
7. **设置**：管理员密码、接码/提炼/支付服务商选择、对应购买链接与 CDK、并发、接码开关和 sub2api 连接设置。

## Quick Start

要求 Python 3.11+：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python run.py
```

默认监听 `127.0.0.1:8000`。首次启动创建配置中的管理员账号；生产环境请立即修改密码并使用随机 JWT 密钥。

## 执行规则

- TXT 原 AT 会保存为任务快照；只有资格明确通过的账号才能进入提炼步骤。
- 提炼与支付服务商独立保存；ytc/Nerver 可作为提炼支付一体服务商，选择综合服务商时两项选择相同；本地提炼参考 [`upi-zero-link`](https://github.com/wangshen233/upi-zero-link)，本地链接可交给已接入的支付服务商。
- 新远程提炼任务必须使用已启用且非空的代理库，并通过 `proxy_source=provided`/`proxies` 传给 ytc；本地提炼按账号使用任务快照代理。
- 接码默认开启，支付成功后无限间隔重试；关闭接码时写入 `paid_without_sms`，不调用 handy。
- 接码成功后保存 CPA/sub2api JSON；每个任务可选择是否按任务快照自动推送，也支持成功库单选/多选后手动推送；真实接码产物和 Refresh Token 必须满足要求。
- sub2api 使用后端 `x-api-key`，Admin Key 不返回前端；推送队列支持幂等、429 等待和结果不确定时只读核对。

## 配置与安全

`config.yaml` 不纳入版本库，真实密钥只通过本地配置或环境变量提供：

| 变量 | 说明 |
|---|---|
| `UPI_DB_PATH` | SQLite 路径 |
| `UPI_JWT_SECRET` | JWT 密钥，生产环境使用随机长密钥 |
| `UPI_ADMIN_USER` / `UPI_ADMIN_PASS` | 首次管理员账号/密码 |
| `UPI_DATA_DIR` | CPA/sub2api 文件保存目录 |
| `UPI_REPORT_DIR` | 任务 Markdown 记录目录 |

代理密码、Admin Key、CDK、账号 TXT、SQLite 和任务产物都必须留在运行环境，不能提交 Git。API 日志、MD 报告和下载接口不返回第三方原始敏感响应。

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
node --test tests/ui_workflow.test.cjs
.venv/bin/python tests/browser_acceptance.py
```

浏览器验收使用隔离 SQLite、隔离文件目录和第三方内存替身，不会消耗真实支付/接码额度，也不会向真实 sub2api 实例发送账号。

## 文档

- [`doc/1.2.0.md`](doc/1.2.0.md)：V1.2.0 需求、服务商分离、本地提炼和界面调整。
- [`doc/供应商.md`](doc/供应商.md)：提炼、支付和接码服务商详情及公开购买入口。
- [`doc/1.1.0.md`](doc/1.1.0.md)：V1.1.0 兼容行为、代理策略和发布验收记录。
- [`doc/1.1.0-PLAN.md`](doc/1.1.0-PLAN.md)：实现计划与边界。
- [`doc/20261012.md`](doc/20261012.md)：V1.0 产品需求；七模块升级说明见 V1.1.0 文档。
- [`doc/IMPLEMENTATION_20261012.md`](doc/IMPLEMENTATION_20261012.md)：V1.0 实现记录及兼容说明。
- [`INTEGRATION.md`](INTEGRATION.md)：ytc、handy 与资格/备用提链接口核验记录。

## 版本

当前源码版本：`1.2.0`。Git `v1.2.0` 标签需待真实第三方联调完成后再创建；当前发布基线标签仍为 `v1.0.0`。
