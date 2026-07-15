# TradingAgents 生产化部署手册

从零把 TradingAgents 部署到 Cloudflare + VPS 的完整路径。第一次
按顺序做需要约 2-3 小时（大部分是等 CF 后台配置生效）。之后每次
迭代 `git push` + `wrangler deploy` 就够了。

## 架构复盘

```
┌────────────┐      ┌──────────────────────────────────────┐
│   User     │─TLS─▶│  Cloudflare Access (SSO gate)         │
└────────────┘      └────────┬─────────────────────────────┘
                             │
                    ┌────────┴────────────┐
                    │                     │
                    ▼                     ▼
        ┌───────────────────┐   ┌──────────────────────┐
        │ Cloudflare Pages  │   │ Workers (Hono API)   │
        │  React SPA        │   │  + D1 + R2 + KV      │
        └───────────────────┘   │  + Cron + DO         │
                                └────────┬─────────────┘
                                         │ HMAC-signed
                                         │ over Tunnel
                                         ▼
                                ┌──────────────────────┐
                                │ VPS (Hetzner CX22)   │
                                │  cloudflared tunnel  │
                                │  Python worker       │
                                │  systemd daemon      │
                                └──────────────────────┘
```

拆分逻辑：
- **Cloudflare 侧**只做轻活（认证、DB 读写、R2 签名、任务入队、SSE 广播）
- **VPS**只做重活（跑 graph、抓数据、LLM 调用），5-15 分钟一个任务
- **Access** 处理 SSO，代码 0 行
- **HMAC** 保护 `/internal/*` 通道，VPS 不暴露公网端口

---

## 目录导览

```
TradingAgents/
├── cf/                       # Cloudflare 侧
│   ├── schema/001_init.sql   # D1 建表 SQL
│   ├── workers/api/          # 主 API worker (Hono)
│   ├── workers/scheduler/    # Cron Trigger (每分钟)
│   ├── workers/job-room/     # Durable Object (SSE fanout)
│   ├── pages/                # React SPA
│   └── README.md
├── worker/                   # VPS 侧 Python 常驻进程
│   ├── daemon.py             # 主循环
│   ├── runner.py             # 跑一个 job
│   ├── cf_client.py          # 调 CF API（HMAC 签名）
│   ├── r2_writer.py          # boto3 写 R2
│   ├── systemd/…             # systemd unit
│   └── README.md
├── tradingagents/            # 核心逻辑（不动）
├── web/                      # 老 FastAPI 代码（保留作参考，将来删）
└── PRODUCTION.md             # 本文件
```

---

## Phase 1 — Cloudflare 侧上线 (~1 小时)

### 1.1 账号 & CLI

```bash
# 一次性：注册 Cloudflare 账号（免费），如果要用自定义域名再加一个 Zone
npm install -g wrangler
wrangler login
```

### 1.2 创建资源

```bash
cd cf

# 1. D1 数据库
wrangler d1 create tradingagents-db
# → 复制输出里的 database_id

# 2. R2 桶
wrangler r2 bucket create tradingagents-reports

# 3. KV namespace（会话缓存 / 幂等锁）
wrangler kv:namespace create SESSIONS
# → 复制 id

# 4. R2 API token（用于 VPS 侧写 R2）
#    到 Cloudflare dashboard → R2 → Manage API Tokens → Create API Token
#    权限选 "Object Read & Write"，作用域限 tradingagents-reports
#    保存 access_key_id + secret_access_key
```

把 `database_id` 和 KV `id` 填到：
- `cf/workers/api/wrangler.toml`
- `cf/workers/scheduler/wrangler.toml`

### 1.3 建表

```bash
wrangler d1 execute tradingagents-db \
  --file=cf/schema/001_init.sql --remote
```

### 1.4 部署三个 worker

```bash
# 顺序重要：Durable Object 先部署，其它 worker 才能绑定它。
cd cf/workers/job-room && npm install && npm run deploy
cd ../scheduler         && npm install && npm run deploy
cd ../api               && npm install && npm run deploy
```

第一次部署 API worker 之前先设置 secrets：

```bash
cd cf/workers/api
# 生成一个随机 token（保存好，VPS 侧也要用）
CF_INTERNAL_TOKEN=$(openssl rand -hex 32)
echo $CF_INTERNAL_TOKEN                            # 记下

wrangler secret put CF_INTERNAL_TOKEN              # 粘贴上面的
wrangler secret put R2_ACCOUNT_ID                  # 从 dashboard 拿
wrangler secret put R2_ACCESS_KEY_ID
wrangler secret put R2_SECRET_ACCESS_KEY
# Stripe 先跳过

wrangler deploy
# → 记下部署后的 URL，形如：
#   https://tradingagents-api.<subdomain>.workers.dev
```

### 1.5 配置 Cloudflare Access（认证）

进 [Zero Trust dashboard](https://one.dash.cloudflare.com) → Access → Applications：

1. **Add an application** → **Self-hosted**
2. 名字：`TradingAgents`
3. 域名：临时先填 `*.pages.dev`（部署 SPA 后再改成正式域名）
4. 加 Policy：
   - Name: `internal-users`
   - Action: Allow
   - Include: `Emails` → 加你和团队成员的邮箱（或 `Emails ending in` → `@yourcompany.com`）
5. **Save**

保存后在 App 设置里找到 **AUD tag**（形如 `abc123def456...`），把它填到：

```toml
# cf/workers/api/wrangler.toml
ACCESS_TEAM_DOMAIN = "your-team.cloudflareaccess.com"
ACCESS_AUD = "abc123def456..."
```

再重新 `wrangler deploy` 一次让 vars 生效。

### 1.6 部署 SPA

```bash
cd cf/pages
npm install
npm run build

wrangler pages project create tradingagents      # 一次性
wrangler pages deploy dist --project-name=tradingagents
# → 拿到 https://tradingagents.pages.dev
```

回 Access 后台把 App 域名改成这个 pages.dev 地址，Access 才会真正拦截 SPA 页面。

### 1.7 验证第一段

浏览器打开 `https://tradingagents.pages.dev`：
- 首次访问会被 Access 拦，登录邮箱走 magic link
- 登录后进入 SPA，页面顶栏能看到导航
- 点"新建分析" → 表单能提交，但提交后 job 会一直卡在 `queued`（因为还没起 VPS worker）

到这里 **Cloudflare 侧就绪**。

---

## Phase 2 — VPS 上线 (~30 分钟)

### 2.1 选机器 & 装系统

**推荐配置**：Hetzner CX22（4G 内存 / 2 vCPU / 40G 磁盘，€3.79/月）
或阿里云轻量应用 4G，或任意云厂商 2C4G 机器。系统装 **Ubuntu 24.04 LTS**。

```bash
ssh root@your-vps

# 基础工具
apt update && apt install -y python3.12 python3.12-venv git curl
```

### 2.2 装 cloudflared（Tunnel 客户端）

VPS 上的 Python worker 需要访问 `https://tradingagents-api.workers.dev`。
理论上直接访问就行；用 Tunnel 的好处是**未来 API 可能加 IP 白名单**时你可以走 Tunnel 打洞而不用改防火墙。

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o /tmp/cf.deb
apt install -y /tmp/cf.deb

# 如果需要 Tunnel（可选）：
# cloudflared tunnel login
# cloudflared tunnel create tradingagents-worker
# 配置见 cloudflared 文档
```

MVP 阶段直接用普通 HTTPS 到 workers.dev 就够，Tunnel 后期再加。

### 2.3 部署代码

```bash
useradd -m -s /bin/bash tradingagents
sudo -u tradingagents git clone https://github.com/<you>/TradingAgents.git /opt/tradingagents
cd /opt/tradingagents
sudo -u tradingagents python3.12 -m venv .venv
sudo -u tradingagents .venv/bin/pip install -e .
sudo -u tradingagents .venv/bin/pip install -r worker/requirements.txt
```

### 2.4 配置环境变量

```bash
cp worker/.env.example /etc/tradingagents-worker.env
chmod 600 /etc/tradingagents-worker.env
vim /etc/tradingagents-worker.env
```

必填项：
```env
CF_API_BASE=https://tradingagents-api.<subdomain>.workers.dev
CF_INTERNAL_TOKEN=<Phase 1.4 里生成的那个>

R2_ACCOUNT_ID=<从 CF dashboard 首页拿>
R2_ACCESS_KEY_ID=<Phase 1.2 里创建的>
R2_SECRET_ACCESS_KEY=<同上>

OPENAI_API_KEY=sk-...
# 或 ANTHROPIC / DEEPSEEK / GOOGLE / FINNHUB
```

### 2.5 systemd 拉起来

```bash
cp /opt/tradingagents/worker/systemd/tradingagents-worker.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tradingagents-worker

systemctl status tradingagents-worker
journalctl -u tradingagents-worker -f
```

看到 `daemon: polling` 类日志说明起来了。

### 2.6 端到端验证

浏览器回到 SPA，提交一次真实分析：
1. 提交任务 → 返回 job_id，页面跳转
2. VPS 日志里 5 秒内看到 `job <id>: starting AAPL @ 2026-07-14`
3. 页面 SSE 事件流开始滚动（chunk 事件）
4. 5-15 分钟后 job 状态变 `done`，能看到"下载完整报告"按钮
5. 点开是 R2 预签名 URL，能下载完整 Markdown

**到这里 MVP 就上线了。**

---

## Phase 3 — 加固 & 迭代（可选）

按你 MVP 计划分周推：

### Week 3.a — 全文搜索

`cf/workers/api/src/routes/jobs.ts` 里的 `GET /` 加 FTS5 查询分支：

```typescript
if (q) {
  const rows = await c.env.DB.prepare(
    `SELECT j.* FROM jobs j
       JOIN reports_fts f ON f.job_id = j.id
      WHERE j.user_id = ? AND reports_fts MATCH ?
      ORDER BY rank`
  ).bind(user.id, q).all();
}
```

runner.py 完成后把 `INSERT INTO reports_fts` 也补上。

### Week 3.b — Stripe 支付

1. Stripe 后台建 Product + Price
2. `cf/workers/api/src/routes/billing.ts` 里 `/checkout` 填实（`stripe.checkout.sessions.create`）
3. `webhooks.ts` 里的 `/stripe` 填实（verify signature + upsert `subscriptions` 表）
4. `wrangler.toml` 里 `STRIPE_ENABLED=true`

在此之前所有用户走 free 额度 + 管理员手动 grant：

```bash
# 给某个邮箱开永久权限
curl https://tradingagents-api.workers.dev/api/billing/admin/grant \
  -H "Cookie: <你的 Access cookie>" \
  -d '{"target_email":"someone@example.com","plan":"admin_bypass","credits":9999}'
```

### Week 4 — 定时任务 UI

`cf/pages/src/pages/Schedules.tsx` 从 stub 替换成真正的 CRUD 界面。
后端 `/api/schedules` 从 501 stub 替换成实现（模式跟 jobs.ts 类似）。

### Week 5 — 通知

- **邮件**：Cloudflare Email Workers（免费）
- **企微/钉钉/飞书**：webhook URL 存 `users.notify_webhook`，`/internal/jobs/:id/finish` 里完工后 fetch 一次

---

## 运维手册

### 查日志

```bash
# VPS 侧
journalctl -u tradingagents-worker -f
journalctl -u tradingagents-worker --since "1 hour ago"

# Workers 侧
wrangler tail tradingagents-api          # 实时
# 或 dashboard → Workers → tradingagents-api → Logs
```

### 常见问题

**Q: 任务卡在 queued 不跑**
- 检查 VPS `systemctl status tradingagents-worker`
- `journalctl -u tradingagents-worker` 看是不是 claim 401（HMAC 签名不匹配 → 检查 CF_INTERNAL_TOKEN 两边一致）
- Workers 侧 `wrangler tail` 看 `/internal/jobs/claim` 有没有被调到

**Q: SSE 事件没实时推**
- Access 会在某些浏览器缓存策略下缓冲 SSE。检查 Access App → Advanced Settings → Same site cookie 设成 `None`
- Chrome DevTools Network 面板看 `/api/jobs/:id/events` 是不是 `text/event-stream`

**Q: R2 存储上限**
- R2 免费额度：10 GB 存储、1M class A / 10M class B 请求/月
- 单份报告 ~30 KB，10 万份才 3 GB，MVP 阶段不用担心
- 满了：升 R2 付费（$0.015/GB/月）或加 lifecycle 规则删 90 天前的

**Q: D1 上限**
- 免费：5M 行读 / 100K 行写 每天，10 GB 存储
- 每次分析写 ~5 行（jobs + reports + FTS + usage_events），单用户一天跑 100 次也才 500 写，绰绰有余
- 超了会看到 `wrangler deploy` 告警，考虑升 Workers Paid ($5/月)

### 回滚

```bash
# Workers
wrangler rollback --name=tradingagents-api

# Pages
# dashboard → Pages → tradingagents → Deployments → 选历史版本 → Rollback
```

D1 没有内建 rollback，靠 schema 迁移向前兼容 + 定期 `wrangler d1 export` 备份。

### 备份

每天 cron 一次（可以放到 GitHub Actions）：

```bash
wrangler d1 export tradingagents-db --output=backup-$(date +%F).sql --remote
aws s3 cp backup-*.sql s3://your-backup-bucket/  # 或者上传到别的 R2 桶
```

---

## 成本估算（月度，人民币）

| 项目 | 说明 | 成本 |
|------|------|------|
| Cloudflare Workers Free | 100K 请求/天 | ¥0 |
| Cloudflare Pages Free | 静态托管 | ¥0 |
| Cloudflare D1 Free | 5M 读 / 10G 存 | ¥0 |
| Cloudflare R2 Free | 10G 存 / 1M 请求 | ¥0 |
| Cloudflare Access Free | 50 seats | ¥0 |
| Hetzner CX22 VPS | 4G 2vCPU | ~¥30 |
| LLM 调用（OpenAI GPT-5.5） | 100 次分析/月 | ~¥300-800 |
| **合计** | | **¥330-830** |

上到 1000 用户/月这个数量级前，Cloudflare 侧基本不掏钱；VPS 换成 8G 机器
（~¥60/月）即可。真正的成本是 LLM，跟每个用户跑几次强相关，所以 free quota
默认给 5 次是合理起点。

---

## 下一步（骨架已完成）

现在项目里已经有：
- `cf/` — 完整 Cloudflare 侧代码（TS）
- `worker/` — 完整 VPS 侧代码（Python）
- `cf/schema/001_init.sql` — 完整 D1 schema
- 所有 wrangler.toml、systemd unit、Dockerfile

**你需要做的**：
1. 走 Phase 1 把 Cloudflare 侧起来（把 wrangler.toml 里的 REPLACE_ME 填上）
2. 起一台 VPS，走 Phase 2
3. 端到端跑通一个 job，然后回来告诉我遇到什么问题

遇到卡点直接把日志/报错贴给我，我改代码或者补文档。
