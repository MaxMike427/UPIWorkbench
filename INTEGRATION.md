# 三方平台接口实测记录（2026-10-12 实际打开网页核验）

> 来源：真实页面抓取，非推测。`cha.nerver.cc` 有公开 `/api` 文档页；
> `apihub.space/handy` 为 SSR 页面，接口从内联 JS 提取；
> `ytc.xxsyun.xyz`（暗光提链/UPI提炼工作台）为 Next.js 纯客户端渲染，从
> `_next/static/chunks/*.js` 提取后端路由与 UI 文案。

---

## 1. cha.nerver.cc — Nerver AC Checker（含资格 + UPI 提链）

- 主站：`https://chong.nerver.cc/`， checker 页 `/`，**公开 API 文档 `/api`**，健康检查 `/healthz`
- 首页资格预检支持：单条 AC Token（`eyJ` 开头 JWT）或整段 `/api/auth/session` JSON，批量混合粘贴

### 1.1 POST /api/v1/check — AC 资格预检（对应 M3）

- 请求：`{ token | tokens[], promoId='plus-1-month-free', check_gcash_pm?, proxy? }`
- 鉴权：`Authorization: Bearer AUTH_TOKEN`（后台未配则可不鉴权）
- 响应主字段：`token_ok`（AC 是否有效）/ `eligible`（是否符合优惠资格）/
  `reason`（empty-token/jwt-expired/token-401/eligible/not-eligible/invalid-token-format…）/
  `coupon_state` / `email` / `account_id` / `plan_type`
- curl：`curl -X POST https://cha.nerver.cc/api/v1/check -H 'content-type: application/json' -d '{"token":"eyJ...","promoId":"plus-1-month-free"}'`

### 1.2 POST /api/v1/upi（+ /upi/stream SSE）— 印度 UPI 提链

- 鉴权：`Bearer <upi_service_token | upi_business_token>`，后台全空 = 不鉴权
- 请求：`{ token(必填), promoId?, poolKeep(1-50)?, poolMax(1-2000)? }`
- 响应：`ok/reason/retryable/message` + `next_action_type` +
  `upi_redirect_url`（授权跳转链接）/ `upi_qr_data` / `upi_qr_image_url` / `upi_hosted_url` +
  `request_id/duration_ms`
- 关键 reason：`approve-blocked` / `no-next-action` = 终态拿不到连接（`retryable=false`，应换号）；
  `account-not-phone` = 后台开了"仅手机号账户"开关而该号是邮箱注册；
  `already-paid/token-invalidated/jwt-expired/unauthorized` 不可重试；
  `concurrency-busy/rate-limited/server-error` 可重试
- **超时：POOL 跑满可达 3–5 分钟，HTTP 超时必须 ≥300s；推荐走 `/upi/stream` SSE**
 （事件：hello → progress[attempt-start/step/approve-tick/approve-hit/attempt-end] → done，每 15s ping 保活）
- ⚠️ **与 ARCHITECTURE.md §1.1 的出入**：文档把"提链"指向 `ytc.xxsyun.xyz`，
  但 `cha.nerver.cc` 自身就有完整 UPI 提链接口，可做备选/对照。

### 1.3 相关接口
- POST /api/v1/subscription（订阅查询，支持 token/session 自动 refresh，返回 plan_type/has_active_subscription）
- POST /api/v1/session/refresh（旧 sessionToken → 新 session，Plus 开通后旧 AT 失效时用）
- POST /api/v1/checkout（Plus 优惠支付链接 pay.openai.com）
- 其它支付通道同源：pix/pix-short/gopay/gcash/ph-link/ideal/blik/sp-pay/paypal（鉴权与响应模式与 upi 同构）

---

## 2. apihub.space/handy — 便捷绑定手机（对应 M4 接码绑定）

- 流程：提交账号 → 自动绑定手机号 → 导出 JSON（codexjson / cpajson / sub2json / codex+cpa / 全部）
- **输入格式：`邮箱----密码----2FA`（四个连字符，每行一个）** —— 与我方 parser 兼容
  （`app/utils/parser.py` 已把 `-{4,}` 归一为 `---`，无需改动）
- CDK：页面顶部额度条绑定，`localStorage` 存；**成功扣 1 次，失败自动退还**；
  融合 CDK（2–10 个合并）；接码 CDK 与提链 CDK **不通用**
- 并发：后台统一下发上限（`simultaneous/max_simultaneous/user_max_simultaneous`），用户在上限内自选 + 每号重试（1–5）
- 手动绑定：取号 → 40s 内轮询短信 → 收到验证码才扣 1 次；40s 未收到才允许换号（服务端先取消旧号）

### 后端接口（相对路径，内联 JS 提取，无公开文档页）
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `api/settings` | `{simultaneous, max_simultaneous, user_max_simultaneous, retries}`（实测：70/10/10/5） |
| GET | `api/cdk?code=` | `{ok,total_uses,available_uses,used_uses,occupied_uses,banned,mergeable}`；失败自动退还已验证 |
| POST | `api/cdk/fuse` | `{codes:[...]}` → `{ok, cdk:{code}, total_uses}`，旧 CDK 作废 |
| POST | `api/jobs` | 表单 `accounts/fmt/cdk_code/user_sim` → `{ids, count, message}`；单行返回单个 id（`hnd_xxx`，已验证） |
| GET | `api/jobs/live?ids=` | → `{jobs:[{id,email,status,progress:{pct,stage,stage_key,detail},result,...}],page,total,stats}`（已验证） |
| POST | `api/jobs/retry` / `api/jobs/delete` | `{ids:[...]}` |
| POST | `api/jobs/zip` | `{mode, ids, fmt}` → zip 二进制 |
| POST | `api/manual/acquire` | 手动取号 |
| POST | `api/manual/check` | 轮询短信 |
| POST | `api/manual/reacquire` | 换号（40s 冷却） |
| POST | `api/manual/cancel` | 取消号码 |

> 实测失败样本（2026-10-12）：`status=fail, progress={pct:16,stage:'失败',stage_key:'fail'}`，
> `result='[failed] SentinelFlowUnavailableError: Sentinel token flow/device_id 不匹配'`——
> 登录阶段平台风控，约 44s 终态，未扣 CDK。

---

## 3. ytc.xxsyun.xyz — 暗光提链 / UPI 提炼工作台（对应 M5 提链生成）

- Next.js SPA（首屏仅 loading，`Workbench` 组件客户端渲染），`<meta description="UPI 提炼工作台">`
- 登录：**账户 API Key**（输入框占位 `ohk_...`）→ 推测 `POST /api/auth/login`
- 提链任务输入：**每行一个 AT，可带 Bearer 前缀** + 并发数；额度不足（"账户额度不足，请先兑换次数"）不可提交
- UPI 执行步骤（UI 文案原文）：连接与账号准备 → 创建结账 → 初始化支付页 →
  核验税区与金额 → 创建 UPI 委托 → 提交确认 → 等待结账授权 → 读取支付链接 → 核验支付链接
- 成功记录页保存提链成功的支付链接（搜邮箱/复制链接/复制 AT/重新提链/批量重试）

### 后端接口（JS 逆向 + Key 实测已验证）
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/login` | `{api_key:'ohk_...'}` → cookie 会话；user `{remaining,available,concurrency_limit}`（实测 remaining=4） |
| POST | `/api/jobs` | 建单 `{tokens(AT换行),concurrency,rounds?,stop_after_first?,auto_submit:true,auto_refresh_expired:true,team_id?}` → `{id}` |
| GET | `/api/jobs?summary=true&page=&limit=50` | 批次列表（含 counts/status/settings） |
| GET | `/api/jobs/{id}?summary=true` | 轮询摘要；终态 `status=succeeded` |
| GET | `/api/jobs/{id}/results` | `{job_id,items:[{account,task_id,url(支付链接),due,expires_at,qr_png,submission}]}`（已验证） |
| GET | `/api/jobs/{id}/export` | 同 results 的 JSON 下载 |
| GET | `/api/jobs/{id}/events?summary=true` | SSE 实时进度 |
| POST | `/api/jobs/{id}/pause|resume|retry|stop` | 批次控制 |
| POST | `/api/jobs/{id}/tasks/{t}/submit|retry|token` | 单任务提交/重试/token |
| POST | `/api/jobs/{id}/tasks/{t}/submission/cancel|refresh` | 提交取消/刷新 |
| GET/POST | `/api/tasks`，POST `/api/tasks/retry`，GET `/api/tasks/tokens` | 任务与 AT |
| POST | `/api/operations/{id}/stop` | 停止操作 |
| POST | `/api/records/clear|clear-failed|restore-failed`，GET `/api/success-records` | 记录管理 |
| GET | `/api/runtime`，`/api/public/activity`，`/api/admin/dashboard|session|proxy-pool` | 运行态/看板 |

---

## 4. 对 UPIWorkbench 设计的影响（M3–M5 待实现）

- M3 资格检查：`QualificationService.check_single` 直接调 `POST {qual_url}/api/v1/check`，
  主看 `token_ok/eligible`，`raw_response` 存整包；批量并发 N 个 + `retryable=false` 直接标记 failed。
  配置键沿用 `qualification.api_url / qualification.api_key`（key 即 Bearer）。
- M4 接码绑定：`SMSService` 以 apihub handy 为 provider#1：
  `POST {handy_base}/api/jobs`（accounts 文本 `邮箱----密码----2FA` 换行拼接 + `fmt=sub2` + `cdk_code`），
  轮询 `api/jobs/live`，`max_retries` 对齐 handy 每号重试；成功导出 sub2json 取新 AT。
  配置键新增 `sms_provider.base_url`（默认 `https://apihub.space/handy`）+ 沿用 `sms_provider.cdk`。
- M5 提链生成：`UPIService` 以 ytc 为 provider#1：
  登录 `ohk_` Key → 创建 job（AT 逐行）→ SSE/轮询 → 取支付链接/二维码。
  配置键新增 `upi_provider.base_url`（默认 `https://ytc.xxsyun.xyz`）+ `upi_provider.api_key`（`ohk_`）。
  **备选**：`cha.nerver.cc POST /api/v1/upi`（Bearer 鉴权、字段齐全、文档公开），建议做 provider#2 做灾备。
- `upi_tasks` 表已有 `upi_link/qr_code_data/raw_response`，够存两家返回；`sms_tasks.provider/order_id`
  可存 `handy/{job_id}`。
