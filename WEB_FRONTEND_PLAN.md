# TradingAgents Web 前端改造计划

> 状态：**M1（P0）+ M2（1.1–1.5）已实施并通过测试**；M3 起（1.6、P2、P3）未开始
> 范围：`web_server.py` + `web/`（新增）+ `static/`（index.html / app.js / style.css）+ `tradingagents/graph/trading_graph.py`
> 更新日期：2026-07-13

---

## 附：四个分析师并行化（核心图重构，超出本计划范围但直接影响体验）

用户反馈"分析太慢"后，另外做了一次**核心分析图重构**：四个分析师（Market/Sentiment/News/Fundamentals）原本在 `graph/setup.py` 里是顺序串联的，改成了从 `START` 并行扇出、在加了 `defer=True` 的 `Bull Researcher` 节点汇聚（LangGraph 不加 `defer` 会在每个分支各自到达时都触发一次汇聚节点，用一个独立脚本验证过这一点）。这不是 Web 前端改动，是 `tradingagents/graph/`、`tradingagents/agents/utils/agent_states.py`、四个分析师文件、`cli/main.py` 状态显示逻辑的改动，CLI 和 Web 共用，一次做完两边都生效。

要点：
- 每个分析师现在有自己独立的消息通道（`market_messages`/`sentiment_messages`/`news_messages`/`fundamentals_messages`），不再共享一份 `messages` 列表——共享列表下并行写入会导致路由逻辑（"最后一条消息是不是我发的"）读错对象。
- 原来在分析师之间"清空共享消息、插入占位符"的 "Msg Clear X" 节点整个移除了；占位符改成在 `propagation.py` 里一次性铺给全部四个新通道。
- 新增 `tests/test_parallel_analyst_graph.py`：静态验证图拓扑（每个分析师直连 START、汇聚节点确实 `defer=True`），动态验证即使四个分析师循环轮数不同（1/1/3/2 轮），汇聚节点也只触发一次且四份报告都在。
- 真实 DeepSeek 端到端跑通验证：见对话记录（此文档不重复贴时间数据，避免和实际测得的数字脱节）。
- 已知遗留风险：如果之前用 `checkpoint_enabled=True` 跑过、且中途崩溃未清理的检查点，理论上可能因为状态结构变了而恢复失败——不常见（默认关闭、单机工具），出现时用 `--clear-checkpoints`（CLI）或删除 `data_cache_dir` 下的 `checkpoints/` 目录即可。

---

## 〇、实施记录（M1 + M2）

本节记录实际落地的实现，供后续 M3+ 的人（或未来的我）核对与衔接。原始规划（一～七节）保持不变作为设计依据；有偏离的地方在此说明原因。

**新增/改动文件**
- `tradingagents/graph/trading_graph.py`：新增 `RunCancelled` 异常；`propagate()`/`_run_graph()` 新增可选参数 `on_chunk`（每个 `graph.stream()` chunk 同步回调一次）与 `should_cancel`（每个 chunk 后轮询，返回 True 则在两个节点之间安全中止）。两者均为可选且默认 `None`，不影响 CLI（`cli/main.py` 走自己的 `graph.graph.stream()` 循环，未使用 `propagate()`）、`main.py`、既有测试的行为。
- `web/`（新包）：
  - `jobs.py` — `Job`（事件日志 + 监听者计数 + 弃用超时取消）、`JobRegistry`（全局唯一 running job 约束）。
  - `pipeline.py` — `PipelineTracker`：把 `graph.stream()` 的累积状态 chunk 转成阶段级 `pending→running→done` 事件，报告文本按前端 DOM 元素 id 打包（`report-market`、`debate-bull` 等），前端收到即可直接调用既有的 `setMarkdown()`，无需额外的字段映射层。
  - `decision.py` — `build_decision_summary()`：5 档 rating 复用已有的 `SignalProcessor.process_signal()`（`agents/utils/rating.py` 的确定性解析，结构化/自由文本两种输出都能兜底）；价位/仓位等字段用锚定正则解析 `render_trader_proposal`/`render_pm_decision`（`schemas.py`）生成的**固定模板**（这段模板是框架自己的代码，不是模型自由发挥的文本，正则可靠；自由文本兜底路径下这些字段合理地退化为 `None`，而不是瞎猜标签）。
  - `routes.py` — FastAPI app：`POST /api/analyze`（202 + job_id，重复提交 409 + 当前 job 信息）、`GET /api/jobs/{id}`、`GET /api/jobs/{id}/events`（SSE，支持 `Last-Event-ID` 重放）、`POST /api/jobs/{id}/cancel`。
- `web_server.py`：瘦身为入口壳（`from web.routes import app` + uvicorn 启动），host/port/reload 均可通过环境变量覆盖。
- `pyproject.toml`：新增 `web` extra（`fastapi`、`uvicorn[standard]`），核心 CLI 安装不受影响。
- `static/index.html`、`style.css`、`app.js`：见下方 P0/P1 表格逐项对照。
- 测试：`tests/test_web_jobs.py`、`tests/test_web_pipeline.py`、`tests/test_web_decision.py`、`tests/test_web_routes.py`（37 个用例，覆盖事件日志、弃用超时、并发 409、取消、完整 SSE 生命周期）。

**与原规划的偏离**
- **0.4（并发隔离）与 0.5（错误内联展示）没有单独打补丁**，而是直接在实现 1.1（Job 模型）时一次做对——`JobRegistry` 天生只允许一个 running job（0.4 的诉求），新版 `app.js` 从一开始就是事件驱动的错误横幅而非 `alert()`（0.5 的诉求）。避免了先打小补丁、几分钟后又为 Job 模型推倒重来。
- **1.5 的"断连自动取消"用了弃用宽限期而非立即取消**：`Job.listener_disconnected()` 只有在监听者数掉到 0 并保持 `_ABANDON_GRACE_SECONDS`（20 秒）之后才置位 `cancel_requested`；纯粹的网络抖动导致 `EventSource` 重连会在宽限期内被 `listener_connected()` 撤销定时器。原规划若做成"连接一断就取消"，会被 EventSource 的正常重连行为误伤，判定为设计缺陷后改成了这个更保守的版本。
- **1.6（运行历史持久化）未实施**——按计划顺延到 M3。

**验证**
- 全量测试：`611 passed, 1 skipped`（跳过项是缺少可选 `langchain_aws` 依赖，与本次改动无关），含新增的 37 个 web 包测试（`test_web_jobs.py`/`test_web_pipeline.py`/`test_web_decision.py`/`test_web_routes.py`），无回归。
- **代码走查中发现并修复一个真实的竞态**：`_run_job` 原本先 `job.finish(...)` 再 `job.emit("result"/"error"/"cancelled", ...)`；但 `Job.wait_for_events_after()` 一旦看到 `is_finished()` 为真就立即返回（哪怕这一刻还没有新事件），SSE 循环因此可能在最终事件真正 `emit` 之前就把连接关掉——前端永远收不到 `result`/`error`/`cancelled`，卡在加载视图。修复：三个分支都改成先 `emit` 后 `finish`。`app.js` 本身在收到终态事件后会主动 `close()`，所以顺序反过来后，SSE 连接最多晚一次 1 秒轮询才关闭，无害。
- **真实端到端跑通**（独立进程，8001 端口，不影响用户已有的 8000 端口实例）：用 DeepSeek（`.env` 里唯一填了真实 key 的 provider；OpenAI/Google/Anthropic/XAI 的 key 在 `.env` 里是空占位符）跑 `AAPL @ 2026-06-01`：
  - SSE 事件序列为 `topology → stage(×9，pending→running→done) → result`，实测中还观察到 Reddit 429（自动退避重试）与 StockTwits 403（地域封锁）两种真实降级路径，分析仍完整跑完。
  - `decision_summary` 在真实模型输出上验证了**两条路径都对**：Portfolio Manager 的结构化输出走了确定性模板（`rating`/`price_target`/`time_horizon`/`executive_summary` 全部正确解析出 "Hold" / 315.0 / "3-6 months" / 完整摘要文本）；同一次运行里 Trader 的输出意外落入自由文本兜底路径（`**Action: HOLD**`，冒号位置与模板不同），`entry_price`/`stop_loss`/`position_sizing` 按设计正确退化为 `None`，而不是误解析——这正是 `decision.py` 要处理的确切场景，线上真实验证了一次。
- **运维提醒**：用户本机原有一个 8000 端口的 `web_server.py` 进程（本次改动前就在跑，PID 60383，已运行 4 小时+），我验证时特意没有碰它、改用 8001 端口跑测试。它跑的是**旧代码**（Python 进程启动时已把代码读进内存，文件改了它不会自动更新），要用上本次的所有改动（Job 模型、实时进度、XSS 修复等）需要用户自己重启它。

---

## 一、现状盘点

当前实现是一个可用的 MVP：FastAPI 单文件后端（`web_server.py`，145 行）+ 无框架原生前端（`static/`）。

**已有能力**
- `POST /api/analyze`：接收 ticker/日期/模型配置，后台线程跑完整分析图，SSE 流式推送日志行，最后一次性推送全部结果。
- 前端三视图切换（欢迎 / 加载 / 结果），五个结果 Tab（分析师报告、多空辩论、风险管理、交易员方案、完整计划），中英双语切换，手写 Markdown 渲染器。

**关键短板（按危害排序）**
1. **XSS 注入面**：`app.js` 的 `renderMarkdown()`/`parseInline()` 不做 HTML 转义直接 `innerHTML`。报告正文包含抓取的新闻标题、Reddit/StockTwits 帖子——外部不可信内容可直接注入脚本。
2. **决策字段靠正则抠 Markdown**：`app.js:173-199` 用 6 个正则匹配 `**Rating**:`、`**Entry Price**:` 等。而后端 `tradingagents/agents/schemas.py` 本来就有结构化的 `TraderProposal`（action / entry_price / stop_loss / position_sizing）和 `PortfolioDecision`（五档 rating / executive_summary / price_target / time_horizon）。模型换个措辞，头部全变 N/A。
3. **无实时进度**：`trading_graph.py:443` 的 debug 路径本就用 `graph.stream()` 逐节点产出状态，但 Web 后端等全跑完才返回。用户盯着日志滚动 1–3 分钟。
4. **无任务模型**：fetch POST 流不可重连；刷新页面 = 丢任务但 LLM 继续烧钱；两个并发请求的日志会互相串流（`QueueHandler` 挂 root logger）。
5. **无历史记录**：`reporting.write_report_tree()` 现成的持久化没被 Web 调用，每次结果关页即失。
6. **前后端配置脱节**：前端硬编码 DeepSeek 默认值，后端 `DEFAULT_CONFIG` 是 openai/gpt-5.5；`llm_clients/model_catalog.py` 的模型目录没有暴露给前端。
7. **CDN 依赖**：`index.html` 引 Google Fonts，本地网络环境下首屏被卡。
8. **不安全默认值**：`0.0.0.0` 绑定 + CORS `*` + `reload=True`。

---

## 二、目标

把"提交表单 → 干等 → 一次性出结果"升级为"**看着智能体团队实时干活**"的专业分析工作台：

1. 逐智能体实时进度与增量报告展示；
2. 决策信息全部来自结构化数据，展示分类清晰（决策头部 / 分析师 / 辩论 / 风控 / 原始数据）；
3. 任务可取消、可重连、可回看历史；
4. 安全（XSS / 网络绑定 / CORS）达到可对外演示的水准。

---

## 三、架构改造设计

### 3.1 Job 模型（替代单请求长流）

```
POST /api/analyze          -> 202 {job_id}          # 立即返回，不再阻塞
GET  /api/jobs/{id}/events -> text/event-stream      # 标准 EventSource，可断线重连
GET  /api/jobs/{id}        -> {status, result?}      # 轮询兜底 / 刷新后恢复
POST /api/jobs/{id}/cancel -> 202                    # 中止执行
```

- 内存级 JobRegistry（dict + 锁）即可，单机单用户场景不引入 Redis/Celery。
- 同时只允许一个 running job（资源与日志隔离的最简解），第二个提交返回 `409 + 当前 job_id`。
- SSE 改用 `EventSource`（GET）后天然支持 `Last-Event-ID` 重连；事件带自增 id，job 保留完整事件缓冲区用于重放。
- 客户端断开检测：EventSource 断开且超时未重连 → 置 cancel 标志；图执行循环（`graph.stream()` 的 for 循环）每个 chunk 检查标志并中止，避免关页后 LLM 继续烧钱。

### 3.2 SSE 事件协议

```jsonc
{"type": "stage",  "agent": "market",    "status": "running"}
{"type": "stage",  "agent": "market",    "status": "done", "elapsed_s": 42.3,
 "report_key": "market_report", "report": "## Market Analysis ..."}
{"type": "log",    "level": "INFO", "message": "..."}          // 沿用现有日志流
{"type": "ping"}                                                // 沿用现有保活
{"type": "result", "data": { ...最终完整结果... }}
{"type": "error",  "message": "...", "stage": "fundamentals"}
```

- 实现方式：后端迭代 `graph.stream(init_state)`，对每个 chunk diff 出新出现的报告字段（`market_report` / `sentiment_report` / `news_report` / `fundamentals_report` / `investment_debate_state.judge_decision` / `trader_investment_plan` / `risk_debate_state.judge_decision` / `final_trade_decision`），映射为 stage 事件。
- 节点拓扑（用于前端流水线图）由后端按 `selected_analysts` 和辩论轮数生成，随 job 创建响应下发，前端不硬编码。

### 3.3 结构化决策响应

`result.data` 增加 `decision_summary` 字段，由后端组装（优先从 state 中的结构化对象取；结构化缺失时后端统一做一次 Markdown 解析，正则只存在于一处 Python 代码而不是散在 JS 里）：

```jsonc
{
  "action": "BUY",                    // TraderAction
  "rating": "MODERATE_BUY",           // PortfolioRating 五档
  "entry_price": 1.52, "stop_loss": 1.31, "price_target": 2.10,
  "position_sizing": "2% of portfolio",
  "time_horizon": "3-6 months",
  "executive_summary": "..."
}
```

---

## 四、任务清单

### P0 — 正确性与安全（预计 0.5–1 天）

| # | 状态 | 任务 | 涉及文件 | 验收标准 |
|---|------|------|---------|---------|
| 0.1 | ✅ 已完成 | Markdown 渲染 XSS 加固：解析前转义 `&<>"'`；或引入本地打包 marked+DOMPurify 至 `static/vendor/` | `app.js` | 报告含 `<img onerror>` / `<script>` 时仅显示为文本 |
| 0.2 | ✅ 已完成 | 决策字段结构化（见 3.3），删除 `app.js` 全部字段正则 | `web/decision.py`, `web/routes.py`, `app.js` | 换任意 LLM 供应商跑一轮，头部六项字段与"决策摘要"均正确填充 |
| 0.3 | ✅ 已完成 | 移除 Google Fonts CDN，本地字体文件或系统字体栈 | `index.html`, `style.css` | 断网打开页面，首屏 < 1s，无挂起请求 |
| 0.4 | ✅ 已完成（并入 1.1） | 并发隔离：全局单 running job 锁，重复提交返回 409 及提示 | `web/jobs.py` | 两个标签页同时提交，第二个得到明确提示且日志不串流 |
| 0.5 | ✅ 已完成（并入 1.1） | 错误内联展示：错误卡片（信息 + 重试按钮 + 保留已收日志），废除 `alert()` | `app.js`, `index.html`, `style.css` | 断网/后端抛错时日志区可回看，可一键重试 |
| 0.6 | ✅ 已完成 | 安全默认值：绑定 `127.0.0.1`、CORS 同源、`reload` 仅开发模式（环境变量开关） | `web_server.py`, `web/routes.py` | 局域网其他机器默认无法访问 |

### P1 — Job 模型与实时进度（预计 2–3 天）

| # | 状态 | 任务 | 涉及文件 | 验收标准 |
|---|------|------|---------|---------|
| 1.1 | ✅ 已完成 | JobRegistry + 四个端点（见 3.1），含事件缓冲与 `Last-Event-ID` 重放 | `web/jobs.py`, `web/routes.py` | 分析中途刷新页面，进度与日志完整恢复 |
| 1.2 | ✅ 已完成 | `graph.stream()` 逐节点事件推送（见 3.2） | `tradingagents/graph/trading_graph.py`（新增 `on_chunk`/`should_cancel`）, `web/pipeline.py` | 每个分析师完成瞬间前端收到对应 stage 事件与报告全文 |
| 1.3 | ✅ 已完成 | 前端"智能体流水线"进度视图：按下发拓扑渲染节点（4 分析师 → 多空辩论 → 研究经理 → 交易员 → 风控辩论 → 组合经理），pending/running/done 三态 + 单节点耗时 | `index.html`, `app.js`, `style.css` | 加载视图能看到当前卡在哪个智能体 |
| 1.4 | ✅ 已完成 | 增量报告渲染：stage 事件到达即填充对应结果卡；新增"预览已生成的报告"按钮 + 结果视图内"分析仍在进行中"横幅，运行中即可切入查看并可返回进度视图 | `app.js`, `index.html`, `style.css` | 无需等待全部完成即可阅读已产出报告 |
| 1.5 | ✅ 已完成 | 取消：UI"停止分析"按钮 + 后端 cancel 标志（两节点之间安全中止）+ 断连超宽限期（20s）自动取消 | `tradingagents/graph/trading_graph.py`（`RunCancelled`）, `web/jobs.py`, `web/routes.py`, `app.js` | 点击停止后 10s 内后端日志停止推进；关闭页面 20s 后同理 |
| 1.6 | ⬜ 未开始 | 运行历史：跑完调用 `reporting.write_report_tree()`；`GET /api/history`、`GET /api/history/{run_id}`；侧边栏历史列表（ticker + 日期 + 决策徽章），点击加载完整结果 | `web_server.py`, `tradingagents/reporting.py`（只读复用）, 前端三件套 | 重启服务后仍能浏览既往全部分析 |

### P2 — 详细信息展示与分类（预计 2–3 天）

| # | 任务 | 说明 | 验收标准 |
|---|------|------|---------|
| 2.1 | 决策头部升级 | 五档 rating 色阶徽章（STRONG_BUY→STRONG_SELL）、time_horizon 展示；风险回报比 =(target−entry)/(entry−stop) 自动计算 | 头部信息全部来自 `decision_summary`，无 N/A 断档 |
| 2.2 | 情绪仪表盘 | 解析 `sentiment_report` 头部 band/score/confidence 渲染 0–10 色阶仪表 + 置信度徽章；四数据源状态 chip（新闻/StockTwits/Reddit/Google Trends），`<... unavailable>` 占位符自动识别为"缺失"标红 | TLRY 实测 StockTwits 403 时 chip 显示红色"不可用" |
| 2.3 | 辩论对话流 | `bull_history`/`bear_history` 按 `Bull Analyst:`/`Bear Analyst:` 前缀与轮次切分，渲染为聊天气泡（多方左绿、空方右红、轮次分隔线）；风控三方用 `history` 字段做交错时间线 | max_debate_rounds=3 时三轮清晰分组、可对照阅读 |
| 2.4 | 分析师卡片信息架构 | 长报告默认折叠为首段摘要 + 展开；`##` 标题生成锚点目录；每卡复制按钮；网格/单列阅读模式切换 | 5000 字报告不再需要在小卡片里滚动 |
| 2.5 | 价格图表 | 新增 `GET /api/price/{ticker}?date=`（复用 `dataflows/y_finance.py`），决策头部下画分析日前后 90 天收盘价（或 K 线），叠加 entry/stop/target 三条水平参考线；实现时按 dataviz 规范取色 | 图上三条线与头部数字一致；无数据时优雅隐藏 |
| 2.6 | Trends sparkline | 情绪卡内嵌 14 天 Google Trends 迷你趋势图（数据已在报告文本中，或后端随 result 附结构化数组） | 有 Trends 数据时显示，占位符时隐藏 |
| 2.7 | 原始数据 Tab | `<pre>` 替换为可折叠 JSON 树 + 完整 final_state 下载按钮 | 任意层级可展开/收起/复制 |

### P3 — 配置面板与打磨（预计 1–2 天）

| # | 任务 | 说明 |
|---|------|------|
| 3.1 | `GET /api/catalog`：暴露 `model_catalog.py` 的 provider→models 注册表与 `DEFAULT_CONFIG` 默认值；前端 provider/模型下拉联动（保留自定义输入） |
| 3.2 | 补齐配置项：分析师多选（`selected_analysts`）、资产类型 stock/crypto（`propagate(asset_type=)`）、输出语言、风控轮数（`max_risk_discuss_rounds`）、temperature |
| 3.3 | i18n 收尾：HTML 硬编码中文文案（"暂无数据"等）纳入 data-zh/data-en；语言选择持久化 localStorage；动态字符串统一走词典对象 |
| 3.4 | 响应式：≤1024px 侧边栏折叠为抽屉；辩论双列/风控三列窄屏堆叠；触屏 Tab 可横滑 |
| 3.5 | 可访问性：键盘焦点样式、aria-label、玻璃拟态文字对比度过 WCAG AA |
| 3.6 | 导出：完整报告导出 Markdown；打印友好样式（`@media print`）以支持另存 PDF |
| 3.7 | 测试：`tests/test_web_server.py`（TestClient + mock `TradingAgentsGraph`：事件序列、409 锁、cancel、历史端点）；Playwright 冒烟一条（提交→进度→结果） |
| 3.8 | favicon + 页面 meta 完善 |

---

## 五、实施顺序与里程碑

```
M1 (P0 全部)          安全可对外演示的基线            ~1 天   ✅ 已完成
M2 (1.1–1.5)          实时流水线 + 可取消/可重连      ~2 天   ✅ 已完成
M3 (1.6 + 2.1–2.3)    历史记录 + 决策/情绪/辩论展示    ~2 天   ⬜ 未开始
M4 (2.4–2.7)          图表与信息架构                  ~1.5 天  ⬜ 未开始
M5 (P3)               配置联动与打磨                  ~1.5 天  ⬜ 未开始
```

依赖关系：2.1 依赖 0.2（结构化决策）；1.3/1.4 依赖 1.1/1.2（Job+事件）；2.5/2.6 建议在 M2 之后做（复用事件通道下发数据）。

## 六、风险与决策点

- ~~**`graph.stream()` 的 chunk 粒度**~~ **已验证**：`stream_mode="values"`（`propagation.py`）下每个 chunk 是完整累积状态而非增量，"字段非空即已完成"是可靠信号（`web/pipeline.py` 采用此假设，真实 DeepSeek 跑通确认）；未使用 `count` 字段判轮次，因为 P1 只做节点级 pending/running/done，不做轮次级细节（那是 P2 2.3 的范围）。
- **checkpoint 恢复与 Job 模型的交互**：`checkpoint_enabled` 时中断的 run 理论上可续跑——本期只保证不冲突（cancel 后 checkpoint 仍完好，`RunCancelled` 在 `_log_state`/`clear_checkpoint` 之前抛出），"从断点续跑"按钮留到下一期。
- ~~**单文件 `web_server.py` 是否拆包**~~ **已拆分**：`web/`（`jobs.py` / `pipeline.py` / `decision.py` / `routes.py`），`web_server.py` 保留为入口壳。
- **前端是否引框架**：维持无框架原生 JS（当前体量可控、零构建步骤符合项目风格）；若 P2 后 `app.js` 超过 ~1500 行再评估 Preact/htm 这类免构建方案，不引入打包器。
- **图表库**：优先手写 SVG sparkline（零依赖）；K 线若手写成本高，备选本地打包 lightweight-charts（~45KB）。

## 七、明确不做（本期）

- 多用户/鉴权体系（单机个人工具定位）
- WebSocket（SSE 足够，单向流）
- 移动端原生适配之外的 PWA/离线缓存
- 回测界面（属于另一个产品面，backtrader 集成另行规划）
