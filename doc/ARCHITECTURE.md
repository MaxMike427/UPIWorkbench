# UPI 工作台 - 架构设计文档

> 版本：v1.0 | 日期：2026-10-12

---

## 1. 系统概览

### 1.1 核心流程
```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│  步骤1：导入  │───▶│  步骤2：资格  │───▶│  步骤3：接码  │───▶│  步骤4：提链  │───▶│  步骤5：导出  │
│  凭证入库    │    │  检查(AT)    │    │  绑定/重试   │    │  生成UPI链接 │    │  凭证导出    │
└─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘
     │                    │                    │                    │                    │
     ▼                    ▼                    ▼                    ▼                    ▼
  txt解析              cha.nerver.cc        apihub.space         ytc.xxsyun.xyz      导出txt/sqlite
  账号/密码/2fa/AT     批量查询             接码平台SDK           一条龙API           账号/密码/2fa/新AT
```

### 1.2 技术栈
| 层级 | 技术 | 说明 |
|------|------|------|
| Web 框架 | **FastAPI** | 异步、自动 OpenAPI、性能好 |
| 数据库 | **SQLite + aiosqlite** | 文件型、零配置、支持异步 |
| 任务��列 | **asyncio + 内存队列** | 轻量、无需 Redis，单机足够 |
| 前端 | **原生 HTML + Alpine.js + Tailwind CDN** | 无构建、轻量、响应式 |
| 认证 | **JWT (HS256)** | 管理员登录、Token 过期刷新 |
| 进程管理 | **systemd / supervisor / nohup** | 生产环境守护 |

---

## 2. 数据库设计

### 2.1 表结构 (db.sqlite)

```sql
-- 管理员账号
CREATE TABLE admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,      -- bcrypt
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 系统配置（键值对）
CREATE TABLE configs (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    description TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 导入的原始凭证
CREATE TABLE credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    password TEXT NOT NULL,
    twofa_secret TEXT,                -- TOTP secret
    access_token TEXT,                -- AT (ey开头)
    source_file TEXT,                 -- 来源文件名
    status TEXT DEFAULT 'pending',    -- pending/qualified/failed/processing/done
    error_msg TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 资格检查记录
CREATE TABLE qualification_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id INTEGER NOT NULL REFERENCES credentials(id),
    at_token TEXT NOT NULL,
    has_qualification BOOLEAN NOT NULL,
    raw_response TEXT,                -- 原始返回
    checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 接码任务
CREATE TABLE sms_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id INTEGER NOT NULL REFERENCES credentials(id),
    phone_number TEXT,                -- 分配的手机号
    sms_code TEXT,                    -- 收到的验证码
    new_access_token TEXT,            -- 绑定成功后的新 AT
    status TEXT DEFAULT 'waiting',    -- waiting/getting_phone/getting_code/bound/failed
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    provider TEXT,                    -- 接码平台标识
    order_id TEXT,                    -- 平台订单号
    error_msg TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 提链任务
CREATE TABLE upi_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id INTEGER NOT NULL REFERENCES credentials(id),
    sms_task_id INTEGER REFERENCES sms_tasks(id),
    amount INTEGER NOT NULL,          -- 金额(paise)
    note TEXT,
    callback_url TEXT,
    upi_link TEXT,                    -- 生成的 UPI 链接
    qr_code_data TEXT,                -- 二维码 base64
    status TEXT DEFAULT 'pending',    -- pending/processing/success/failed
    raw_response TEXT,
    error_msg TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 任务批次（用于进度聚合）
CREATE TABLE task_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,               -- 批次名称
    total_credentials INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running',    -- running/paused/completed/failed
    current_step TEXT,                -- 当前执行步骤
    progress_json TEXT,               -- JSON: 各步骤计数
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

-- 操作日志
CREATE TABLE audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER REFERENCES admins(id),
    action TEXT NOT NULL,             -- login/import/start_task/export/config_change
    target_type TEXT,                 -- credentials/sms_task/upi_task/config
    target_id INTEGER,
    detail TEXT,
    ip TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 索引
CREATE INDEX idx_credentials_status ON credentials(status);
CREATE INDEX idx_sms_tasks_credential ON sms_tasks(credential_id);
CREATE INDEX idx_upi_tasks_credential ON upi_tasks(credential_id);
CREATE INDEX idx_batches_status ON task_batches(status);
```

---

## 3. 核心模块设计

### 3.1 模块划分
```
upi-workbench/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI 入口
│   ├── config.py               # 配置加载/保存
│   ├── database.py             # 数据库连接/初始化
│   ├── models.py               # Pydantic 模型
│   ├── auth.py                 # JWT 认证
│   ├── services/
│   │   ├── __init__.py
│   │   ├── credential_service.py    # 凭证导入/解析/导出
│   │   ├── qualification_service.py # 资格检查
│   │   ├── sms_service.py           # 接码绑定
│   │   ├── upi_service.py           # 提链生成
│   │   └── batch_service.py         # 批次编排/进度
│   ├── api/
│   │   ├── __init__.py
│   │   ├── auth_router.py
│   │   ├── credential_router.py
│   │   ├── batch_router.py
│   │   ├── config_router.py
│   │   └── dashboard_router.py
│   └── utils/
│       ├── __init__.py
│       ├── parser.py             # txt 解析器
│       ├── crypto.py             # 密码哈希/JWT
│       └── helpers.py
├── static/
│   ├── index.html                # SPA 入口
│   ├── css/style.css
│   └── js/app.js
├── templates/                    # 可选：Jinja2 模板
├── data/
│   └── db.sqlite                 # 数据库文件
├── logs/
├── requirements.txt
├── config.example.yaml
└── run.py                        # 启动脚本
```

### 3.2 关键服务接口

#### CredentialService
```python
class CredentialService:
    async def import_from_txt(self, content: str, filename: str) -> ImportResult
    async def export_credentials(self, status_filter: str = None) -> str  # 返回 txt 内容
    async def get_credentials(self, filters: CredentialFilter) -> Page[Credential]
    async def update_status(self, cid: int, status: str, error: str = None)
```

#### QualificationService
```python
class QualificationService:
    async def check_single(self, at_token: str) -> bool
    async def check_batch(self, credential_ids: List[int]) -> CheckResult
    # 内部：调用 cha.nerver.cc API
```

#### SMSService
```python
class SMSService:
    async def bind_phone(self, credential_id: int) -> SMSTask
    async def poll_sms_code(self, task_id: int) -> str | None
    async def retry_failed(self, task_id: int) -> SMSTask
    # 内部：对接 apihub.space / 其他接码平台
```

#### UPIService
```python
class UPIService:
    async def create_upi_link(self, credential_id: int, amount: int, note: str) -> UPITask
    # 内部：调用 ytc.xxsyun.xyz 一条龙 API
```

#### BatchService (编排器)
```python
class BatchService:
    async def create_batch(self, name: str, credential_ids: List[int]) -> Batch
    async def run_batch(self, batch_id: int)  # 后台异步运行全流程
    async def pause_batch(self, batch_id: int)
    async def resume_batch(self, batch_id: int)
    async def get_progress(self, batch_id: int) -> BatchProgress
```

---

## 4. API 设计 (RESTful)

### 4.1 认证
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/login` | 用户名密码登录，返回 JWT |
| POST | `/api/auth/refresh` | 刷新 Token |
| GET | `/api/auth/me` | 获取当前用户信息 |

### 4.2 凭证管理
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/credentials/import` | 上传 txt 文件导入 |
| GET | `/api/credentials` | 分页查询（支持状态筛选） |
| GET | `/api/credentials/export` | 导出 txt |
| DELETE | `/api/credentials/{id}` | 删除单条 |
| POST | `/api/credentials/batch-delete` | 批量删除 |

### 4.3 批次/任务编排
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/batches` | 创建批次（指定凭证 IDs） |
| GET | `/api/batches` | 列表分页 |
| GET | `/api/batches/{id}/progress` | 实时进度（SSE 或轮询） |
| POST | `/api/batches/{id}/start` | 启动全流程 |
| POST | `/api/batches/{id}/pause` | 暂停 |
| POST | `/api/batches/{id}/resume` | 继续 |
| POST | `/api/batches/{id}/retry-failed` | 重试失败项 |

### 4.4 配置管理
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/configs` | 获取所有配置（脱敏） |
| PUT | `/api/configs/{key}` | 更新单项 |
| POST | `/api/configs/batch` | 批量更新 |

**配置键名规范**：
```
sms_provider.cdk           # 接码平台 CDK
upi_provider.cdk           # 一条龙提链 CDK
qualification.api_url      # 资格检查 API 地址
qualification.api_key      # 资格检查 API Key
app.admin_username         # 管理员用户名（初始化用）
app.admin_password         # 管理员密码（初始化用，存 hash）
```

---

## 5. 前端页面结构 (SPA)

### 5.1 路由/视图
```
├── /login                  # 登录页
├── /dashboard              # 仪表盘：总览统计、最近批次
├── /credentials            # 凭证管理：导入、列表、导出、筛选
├── /batches                # 批次管理：创建、列表、进度详情
│   └── /batches/{id}       # 批次详情：步骤进度条、实时日志、操作按钮
├── /settings               # 系统设置：各平台 CDK、API 地址
└── /logs                   # 操作日志
```

### 5.2 关键交互
- **导入弹窗**：拖拽上传 txt → 预览解析行数 → 确认入库
- **批次创建**：勾选凭证 → 输入批次名/金额/备注 → 创建并自动开始
- **进度页**：WebSocket/SSE 推送实时进度，步骤可视化（待机/进行中/成功/失败）
- **设置页**：表单保存，CDK 显示为 `****`，点击显明文

---

## 6. 异步任务执行模型

### 6.1 任务队列
```python
# 单机内存队列，生产环境可换 Redis/RQ
task_queue: asyncio.Queue[TaskEnvelope]

class TaskEnvelope:
    batch_id: int
    step: Literal['qualification', 'sms', 'upi']
    credential_ids: List[int]
    priority: int = 0
```

### 6.2 Worker 协程
```python
async def worker_loop():
    while True:
        envelope = await task_queue.get()
        try:
            await dispatch_step(envelope)
        except Exception as e:
            await handle_failure(envelope, e)
        finally:
            task_queue.task_done()
```

### 6.3 步骤执行流
```
批次启动
    │
    ├─▶ Step 1: 资格检查 (并发 N 个)
    │      成功 → 标记 qualified，入队 Step 2
    │      失败 → 标记 failed，记录错误
    │
    ├─▶ Step 2: 接码绑定 (串行/小并发，避免风控)
    │      获取手机号 → 提交绑定 → 轮询短信 (最长 120s)
    │      成功 → 存新 AT，入队 Step 3
    │      失败 → 重试 (max_retries)，耗尽 → 标记 failed
    │
    └─▶ Step 3: 提链生成
           调用一条龙 API → 得到 UPI 链接/二维码
           成功 → 标记 done
           失败 → 记录错误，可重试
```

---

## 7. 安全与合规

| 项 | 措施 |
|----|------|
| 密码存储 | bcrypt (cost=12) |
| JWT | HS256，15min access + 7d refresh，HttpOnly Cookie |
| 敏感配置 | 数据库加密存储（可选 Fernet），前端脱敏显示 |
| 操作审计 | 所有写操作记录 audit_logs |
| 速率限制 | 接码/提链 API 调用加随机抖动、指数退避 |
| 数据��离 | SQLite 文件权限 600，定期备份 |

---

## 8. 部署指南

### 8.1 环境要求
- Python 3.11+
- SQLite 3.35+
- 2GB+ RAM（批量时内存占用主要在队列和并发连接）

### 8.2 启动方式
```bash
# 开发
python run.py

# 生产 (systemd)
# /etc/systemd/system/upi-workbench.service
[Unit]
Description=UPI Workbench
After=network.target

[Service]
Type=simple
User=upi
WorkingDirectory=/opt/upi-workbench
ExecStart=/opt/upi-workbench/.venv/bin/python run.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 8.3 反向代理 (Nginx)
```nginx
server {
    listen 80;
    server_name upi.local;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

---

## 9. 扩展性预留

| 方向 | 设计点 |
|------|--------|
| 多接码平台 | `SMSService` 策略模式，配置 `provider` 动态加载 |
| 分布式 Worker | 队列换 Redis/RQ，BatchService 无状态化 |
| Webhook 回调 | UPITask 增加 `callback_url`，成功时 POST |
| 多租户 | 引入 `tenant_id`，Admin 变为超管/租管两级 |
| 监控告警 | 接入 Prometheus `/metrics`，Grafana 看板 |

---

## 10. 开发里程碑

| 阶段 | 交付物 | 预估工时 |
|------|--------|----------|
| M1: 基础设施 | DB、Auth、Config、基础 CRUD | 4h |
| M2: 凭证导入/导出 | txt 解析、入库、导出、Web UI | 3h |
| M3: 资格检查 | cha.nerver.cc 对接、批量检查、进度 | 3h |
| M4: 接码绑定 | apihub.space SDK、重试逻辑、新AT存储 | 4h |
| M5: 提链生成 | ytc.xxsyun.xyz 对接、二维码、结果存储 | 3h |
| M6: 批次编排 | 全流程串联、暂停/继续、实时进度 SSE | 4h |
| M7: Web UI 完善 | 仪表盘、设置页、日志、导出、响应式 | 4h |
| **合计** | **完整可用系统** | **~25h** |

---

## 11. 风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| 第三方 API 变更/封禁 | 核心流程中断 | 抽象适配器层、多平台备选、健康检查 |
| 接码平台余额不足 | 绑定失败 | 配置余额阈值告警、自动暂停批次 |
| 账号风控/封号 | 资源损耗 | IP 池轮换、设备指纹伪装、频率控制 |
| 数据库锁竞争 | 并发性能下降 | WAL 模式、合理索引、批量写入合并事务 |
| 前端长轮询占用连接 | 并发受限 | 优先用 SSE/WebSocket，或缩短轮询间隔 |

---

> **文档维护**：重大变更同步更新此文档，代码注释以此为准。