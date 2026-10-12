# 脱敏源码发布说明

## 发布目录

源码位于 `/root/code/dh/Github/UPIWorkbench`。

## 已排除内容

- SQLite 数据库及 WAL/SHM 文件
- 原始账号 TXT、密码、2FA、访问令牌
- 真实 CPA/sub2api JSON
- 任务执行 Markdown 报告
- 运行日志
- `.venv`、缓存和 `.pyc`
- `config.yaml`、环境变量和真实 CDK/API Key

## 链接保留规则

产品指定的购买和操作入口属于公开页面链接，已保留在 `static/index.html`：

- 接码 CDK 购买：`https://wzyp.cn/shop/8Q9RHH0W`
- 接码 CDK 融合：`https://apihub.space/handy/`
- 提链支付 CDK 购买：`https://wzyp.cn/shop/Camellia`
- 提链 CDK 提交：`https://upi.xxsyun.xyz/employer/tickets/`
- 账号库 0217 购买：`https://wzyp.cn/shop/CPZF8PBK`
- 账号库 AI 批发商：`https://wzyp.cn/shop/8UOIIRW2`

这些链接使用新标签页和 `noopener noreferrer`，不会附带用户输入的密码、令牌或 CDK。

## 发布前检查

```bash
grep -RInE --exclude-dir=.venv --exclude-dir=__pycache__ \
  --exclude='*.sqlite' --exclude='*.log' \
  'HPH-|ohk_[A-Za-z0-9]|eyJ[A-Za-z0-9._-]{20,}' .
pytest tests/ -q
node --test tests/ui_workflow.test.cjs
```

上面的敏感值扫描应无真实凭证命中；`config.example.yaml` 中只保留空值或占位符。
