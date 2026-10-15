# UPI Workbench

账号任务工作台：导入 TXT 账号 → 资格检查 → 提炼 → 支付 → 接码绑定（可关闭）→ 成功库导出，并可通过外部代理库和 sub2api 集成扩展流程。

本项目使用 FastAPI、SQLite 和原生 Alpine.js 页面。发布源码不包含运行数据库、账号 TXT、任务报告、JSON 产物、日志、虚拟环境或真实 CDK/API Key。

## 工作区

V1.3.0 共七个模块：

1. **仪表盘**：账号统计、全流程成功/已成功未接码统计、实时终端日志。
2. **账号库**：TXT 导入、来源文件分组筛选、搜索、分页、跨页选择、精确创建任务和账号删除；仅保留供应商下拉与一个购买入口。
3. **代理库**：添加、导入、测试和批量删除代理；支持 HTTP/HTTPS/SOCKS4/SOCKS5 与带认证代理。新增 711proxy API URL 配置、手动拉取与新任务启动前自动取链；保存不调用真实 API。动态网关支持独立粘性会话与多出口采样，远程 ytc 仅使用 HTTP/HTTPS/SOCKS5。
4. **任务管理**：资格检查、独立提炼/支付服务商、可选接码、按任务自动推送开关、暂停/继续、详情、失败重试和 MD/TXT 导出。
5. **成功库**：区分全流程成功与已成功未接码；后者仅可导出 TXT，不生成伪造 JSON。
6. **集成**：配置 sub2api 推送模板、读取分组、查看队列、逐条重试或只读核对。
7. **设置**：管理员密码、统一供应商配置向导、配置清单与凭证购买/兑换/保存位置、并发和 sub2api 连接设置。

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
- 供应商向导依次选择：提链类型与供应商 → 系统/供应商代理 → 独立支付（非一体供应商）→ 是否接码与供应商 → 凭证 → 确认清单。上一步可重选，取消不修改配置。当前向导开放 ytc + Handy（或不接码），其它供应商仅占位；旧版适配器和历史任务继续兼容。
- ytc 供应商代理明确发送 `proxy_source=internal`，系统代理发送 `provided`。旧配置的 `auto` 行为保留兼容。ytc 必须填写已兑换的 `ohk_` 账户 API Key，不能把购买后未兑换的 CDK 当作登录密钥。
- 启用 711proxy 后，系统代理新任务首次启动前取链，失败阻止提交；已有订单继续使用原成员代理和任务快照，不自动切换出口。请在控制台生成 TXT 换行或 JSON 提取 URL，工作台协议选择与控制台一致；白名单应放行实际使用代理的主机，传给远程供应商前需确认可访问。控制台粘性会话时长限制仍然适用。
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

供应商凭证保存在运行 SQLite 的 `configs` 表，键为 `provider_credentials.<供应商 ID>.cdk`；711proxy 提取 URL 为 `proxy.dynamic.api_url`。设置页显示实际数据库路径、配置键及已保存状态，不回显真实值。切换供应商按各自已保存的凭证读取，留空保留原值；旧凭证在第一次确认向导时迁移。API URL 中的鉴权参数按密钥脱敏。

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
node --test tests/ui_workflow.test.cjs
.venv/bin/python tests/browser_acceptance.py
```

浏览器验收使用隔离 SQLite、隔离文件目录和第三方内存替身，不会消耗真实支付/接码额度，也不会向真实 sub2api 实例发送账号。

## 文档

- [`doc/1.3.0.md`](doc/1.3.0.md)：分步供应商配置、来源筛选、711proxy 动态取链与接口说明。
- [`doc/1.3.0-payment-providers.md`](doc/1.3.0-payment-providers.md)：GitHub 本地提链、wangyou 与 UPI 雇主中心支付组合及安全边界。
- [`doc/1.2.0.md`](doc/1.2.0.md)：V1.2.0 需求、服务商分离、本地提炼和界面调整。
- [`doc/供应商.md`](doc/供应商.md)：提炼、支付和接码服务商详情及公开购买入口。
- [`doc/1.1.0.md`](doc/1.1.0.md)：V1.1.0 兼容行为、代理策略和发布验收记录。
- [`doc/1.1.0-PLAN.md`](doc/1.1.0-PLAN.md)：实现计划与边界。
- [`doc/20261012.md`](doc/20261012.md)：V1.0 产品需求；七模块升级说明见 V1.1.0 文档。
- [`doc/IMPLEMENTATION_20261012.md`](doc/IMPLEMENTATION_20261012.md)：V1.0 实现记录及兼容说明。
- [`INTEGRATION.md`](INTEGRATION.md)：ytc、handy 与资格/备用提链接口核验记录。

## 版本

当前源码版本：`1.3.0`。本次没有创建 Git 发布标签；真实供应商成功链路及 711proxy 鉴权取链仍需专用测试凭证和扣费授权，隔离验收不会消耗真实额度。
