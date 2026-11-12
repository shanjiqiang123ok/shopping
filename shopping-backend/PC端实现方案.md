# 实现方案.md — 前端页面（导购对话调试界面）

> 本方案只覆盖**前端页面**这一项新增功能；后端业务链路见 `../DEVELOPMENT_PLAN.md`。
> 编写时间：2026-11-11。**v1 已实现并通过测试**，因此本文件同时是 v1 的设计说明与 v2 的施工单。

---

## 一、先厘清一个前提：这个页面不是生产前端

| | 界面归属 | 本机可用 | 说明 |
| --- | --- | --- | --- |
| 生产对话界面 | **MaxKB** | ❌（需 Docker，本机没装） | 按 `frame.md`，聊天 UI、用户登录、会话历史、RAG、模型网关都在 MaxKB 侧；本服务只是它的一个"外部 HTTP 工具" |
| **本页面** | shopping-backend | ✅ | **联调工具**：在没有 MaxKB 的情况下，用浏览器直接验证"提问 → 商品 → 库存 → 券 → 话术"整条链路，并把护栏/路由的内部证据摊开给你看 |
| Swagger | FastAPI 自带 | ✅ | `http://127.0.0.1:8000/docs`，能发请求，但没有对话体感和调试字段可视化 |

结论：本页面定位是"带调试面板的临时对话页"，**不能**当成对外产品界面去迭代视觉；等 MaxKB 可用，正式界面立刻由 MaxKB 承担，本页面退回排障工具角色。这条定位决定了下面所有取舍（零构建、单文件、不上路由框架）。

---

## 二、目标与非目标

### 2.1 v1 目标（已完成 ✅）

| 目标 | 验收方式 | 结果 |
| --- | --- | --- |
| 一条命令起服务后可在浏览器连续提问 | `GET /chat` 返回 HTML | ✅ |
| 保持 `user_id` + `session_id`，让"第二款有货吗"这类续问真的生效 | 页面默认沿用同一 session，"新会话"按钮重置 | ✅ |
| 把内部证据摊开：`route_trace`、`degraded`、`counts`、`error_msg`、`latency_ms`、`thread_id` | 每条回答下方的调试面板 | ✅ |
| 预置验证问句，一点即发（对应 `docs/SHOPPING_RULES.md` §9 的用例） | 顶部快捷按钮 8 条 | ✅ |
| 不污染给 MaxKB 的接口契约 | `/chat` 设 `include_in_schema=False`，有用例断言它不出现在 `openapi.json` | ✅ `test_chat_page_stays_out_of_the_maxkb_contract` |
| 零构建、零新依赖 | 单个 `chat.html` + 一条路由，无 Node/npm/打包 | ✅ |

### 2.2 明确不做（避免范围膨胀）

- ❌ 不做商品图瀑布流、营销活动页、购物车页面 —— 这些是 MaxKB 或真实商城前端的职责
- ❌ 不引入 React/Vue/Vite/Tailwind：一旦引入就要求 Node 构建链，与"本机先跑通业务"目标冲突；且这个页面的价值在于**调试可见性**，不在视觉
- ❌ 不在页面里做业务判断（例如前端自己算券该不该展示）—— 一切口径以后端 `services/` 为准，否则会出现两套规则
- ❌ 不做用户注册登录：鉴权由 MaxKB 负责，本页只带一个服务间 token
- ❌ 不接第三方统计/CDN/字体：页面只发同源请求

---

## 三、选型：为什么继续用"挂在 FastAPI 上的单文件页面"

| 方案 | 成本 | 好处 | 代价 | 结论 |
| --- | --- | --- | --- | --- |
| **A. 单文件 HTML 挂 FastAPI `/chat`** | 极低（1 文件 + 1 路由） | 起后端就有，不需要额外端口/进程/CORS；调试字段想加就加 | 手写 DOM，页面变大后要自己拆 | ✅ **采用**，v1 已落地 |
| B. 独立前端工程（Vite + 框架） | 高（Node 工具链、双进程、CORS、构建产物部署） | 适合长期多人维护的正式界面 | 与"无 Docker、先跑通业务"的阶段目标不匹配；排障链多一跳 | ⏳ 仅当要把它做成正式产品界面时再启动 |
| C. 只用 Swagger | 零 | 无需维护 | 无对话体感、无 route_trace 可视化、续问要手填 session | 保留作为接口级排障入口 |
| D. 直接部署 MaxKB | 需 Docker | 就是生产形态 | 本机跑不起来，阻塞验证 | 目标形态，非替代关系 |

**演进余地**（这条是 A 方案的关键前提）：页面只依赖 `POST /api/shopping-agent/run` 的**响应契约**（`answer` / `data` / `meta`，见 `docs/API.md` §2.1），不依赖任何内部实现。将来若要换成 B 方案的正式前端，替换的是页面本身，接口和后端代码零改动。

---

## 四、页面设计

### 4.1 布局

```
┌───────────────────────────────────────────────────────────────┐
│ 标题  [状态文本]        token[____] user[____] session[____]   │  ← header：身份与连接状态
│                                     [新会话] [清屏]            │
├───────────────────────────────────────────────────────────────┤
│ [推荐 2000 元以内的手机] […有券吗] [Redmi…有货吗] [有什么满减]  │  ← 快捷问句（8 条，覆盖分支）
│ [推荐5000元以内电视] [第二款有货吗] [帮我下单买第一个]          │
├───────────────────────────────────────────────────────────────┤
│                                                               │
│   我 ┊ 推荐 2000 元以内的手机                                   │
│      ┌─────────────────────────────────────────┐              │
│      │ Agent：为你找到 3 款 2000 元以内的手机…  │              │
│      │ ── 调试面板 ──────────────────────────── │              │
│      │ route_trace: supervisor│goods│stock│sum  │              │
│      │ intent:goods_query  degraded:false        │              │
│      │ goods:3 coupons:0 dropped_rows:0          │              │
│      │ grounding_drops:0  19ms  rule_based/inproc│              │
│      │ # 商品 价格(分) 可售 券码                  │              │
│      │ 1 realme… 159900 2 -                       │              │
│      │ thread_id: default:s_xxx                   │              │
│      └─────────────────────────────────────────┘              │
├───────────────────────────────────────────────────────────────┤
│ 输入框───────────────────────────────────────  [发送]         │
└───────────────────────────────────────────────────────────────┘
底部一行说明：本页仅本机联调用，同源请求，正式界面由 MaxKB 提供
```

### 4.2 交互流程

```
输入/点快捷问句
  → 读 token / user / session（三者改动即写 localStorage，刷新后延续）
  → POST /api/shopping-agent/run   {user_id, session_id, query}
       Header: Authorization: Bearer <token>
  → HTTP 200：正文写 answer；调试面板按 meta/data 渲染
  → HTTP 401：提示"token 不对"，并保留请求内容方便重试
  → HTTP 422：把 errors[] 拼成 "字段路径: 原因" 逐条显示
  → fetch 异常（服务没起）：显示"请求失败：<原因>"，状态标红
```

`session_id` 是**功能开关**而不是装饰：它等于后端 checkpoint 的 `thread_id` 组成部分，同 session 连问才会命中上一轮候选商品（用例 7）。"新会话"按钮换一个随机 id，就是显式让 checkpoint 换新线程。

### 4.3 调试面板字段（这是页面存在的理由）

| 展示项 | 来源 | 非正常值意味着 |
| --- | --- | --- |
| `route_trace` 节点链条 | `meta.route_trace` | 缺 `stock_worker` → 商品无候选；多出 `coupon_worker` → 缺货判定或券意图触发 |
| `degraded` | `meta.degraded` | `true` → `answer` 是安全话术，不是真答案 |
| `counts.dropped_rows` | 后端 L4 计数 | >0 → 数据源返回了不合契约的行 |
| `counts.grounding_drops` | 后端 L7 计数 | **>0 必须排查**：有话术内容在业务库里无出处 |
| `error_msg` | `meta.error_msg` | 失败原因中文直述 |
| `latency_ms` / `mode` / `backend` | `meta` | 慢请求、驱动来源一目了然 |
| 商品表格（title/price_cents/available/券码） | `data.goods` | 与页面话术对照，肉眼核对"说的数就是库里的数" |

### 4.4 身份与存储

| 项 | 存储 | 备注 |
| --- | --- | --- |
| `AGENT_INTERNAL_TOKEN` | `localStorage.sb_token`，默认 `dev-token` | 本机联调凭证，**不是**生产密钥；页面提供输入框而非硬编码，便于换 token |
| `user_id` | `localStorage.sb_user` | 用来验证偏好读写回（同 user 二次提问可见 `preference_summary` 变化） |
| `session_id` | `localStorage.sb_session` | 控制 checkpoint 线程连续性 |

---

## 五、后端需要配合什么

**结论：v1 不需要新增任何业务接口。** 只加了 2 处：

| 位置 | 改动 | 原因 |
| --- | --- | --- |
| `agent_api/main.py` | `GET /chat` 返回页面 HTML，`include_in_schema=False` | 同源提供，免 CORS；不进 OpenAPI，避免被当成 MaxKB 工具契约的一部分 |
| `agent_api/main.py` | `GET /` 索引里加 `"chat": "/chat"` | 起服务后能发现入口 |

页面文件放在 `src/shopping_backend/web/chat.html`，**随包发布**（和 seeds 同理），因此 `pip/uv` 安装后依然可用；文件缺失时 `/chat` 返回 500 + 一句人话提示，而不是启动失败。

v2 可选（都不影响现有链路）：

| 候选接口 | 用途 | 成本 |
| --- | --- | --- |
| `GET /api/shopping-agent/state?session_id=` | 页面加"查当前会话快照"按钮，直接看 checkpoint 里存了什么（排查续问问题时最有用） | 低，方法在 `runtime` 里已有 |
| `POST /api/shopping-agent/run/stream`（SSE） | 逐节点推进度（`supervisor` → `goods_worker` → …），配合 `AGENT_MODE=llm` 时能看到卡顿在哪一步 | 中，属 M7 项 |

---

## 六、安全与正确性要点（逐条实现，不是原则口号）

| # | 要点 | 做法 |
| --- | --- | --- |
| S1 | **XSS**：回答文本、商品标题、券名一律不可作为 HTML 解析 | 全部走 `textContent` 建节点，代码里没有一处 `innerHTML=` 拼接外部数据 |
| S2 | 只发同源请求 | `fetch("/api/shopping-agent/run")` 相对路径；页面里不出现任何外部域名/CDN |
| S3 | token 泄露面 | 明确标注是"本机联调凭证"；生产由 MaxKB 侧配置密钥，本页面不进入公网部署清单。若你需要更保守，v2 可改为 `sessionStorage`（关标签页即失效） |
| S4 | 输入法回车误发 | **v1 已知缺陷**：中文输入法候选确认的 Enter 会触发 `keydown` 直接发送 → v2 修 `if (event.key === "Enter" && !event.isComposing)`（F2-1） |
| S5 | 并发点击 | `busy` 标志 + 发送按钮 disable，避免同一 session 并发写 checkpoint 造成竞态 |
| S6 | 长会话历史 | 页面历史只在内存，刷新即清；后端 checkpoint 才是持久真相。页面底部提示"刷新后记录清空，续问仍有效"，避免误解为数据丢失 |
| S7 | 大模型模式下的慢响应 | 现在整段等待，超时统一显示 `请求失败`；SSE 化属 v2 的 F5 |

---

## 七、施工单（v2）

| 编号 | 事项 | 说明 | 工时 |
| --- | --- | --- | --- |
| **F2-1** | 修输入法回车 | `!event.isComposing` 判定；同时加 `Shift+Enter` 换行 | 0.1 天 |
| **F2-2** | 会话历史本地留存 | `localStorage.sb_history` 存最近 50 条（含 `meta`），刷新后能复现上次对话用于对比；提供"仅清屏/清历史"两个动作 | 0.3 天 |
| **F2-3** | 分支自检面板 | 一键盘点 7 条 `SHOPPING_RULES.md` §9 用例的 `route_trace` 是否符合预期（把页面变成"能自己跑的验收单"）；后端已有用例，页面做的是同口径的可视化 | 0.4 天 |
| **F2-4** | `grounding_drops` 告警强化 | 非 0 时整条气泡标红并给出排查指引（看 `logs/shopping-dev.jsonl` 的 `guardrail_L7_dropped_lines`） | 0.1 天 |
| **F2-5** | `GET /api/shopping-agent/state` + 页面"查快照"按钮 | 续问出问题时直接看 `seen_goods_ids`/`candidate_memory` | 0.3 天 |
| **F2-6** | 移动端可读 | 现有 viewport 基础上收窄气泡、字段折行 | 0.2 天 |
| **F2-7** | （可选）SSE 流式与逐节点进度 | 需后端先做 `run/stream`（M7） | 1 天 |

建议顺序：**F2-1 → F2-4 → F2-3 → F2-2 → F2-6 → F2-5 →（F2-7 单独排期）**。前四项都不动后端，风险最低，能最快把页面变成"自证的验收工具"。

---

## 八、验收标准

### 8.1 已有自动化（v1）

```
uv run pytest tests/test_agent_api.py -q
```

| 用例 | 断言 |
| --- | --- |
| `test_chat_debug_page_is_served` | `/chat` 200、`text/html`、含 `/api/shopping-agent/run`、含 `route_trace`、索引里列出 `chat` |
| `test_chat_page_stays_out_of_the_maxkb_contract` | `/chat` 不在 `openapi.json` 的 paths 里 |

页面是纯静态 HTML，不测视觉，只测"服务确实把它吐出来了 + 没污染对外契约"这两个真风险点。

### 8.2 手工清单（每次动页面后照做一遍，5 分钟）

```bash
uv run python -m uvicorn shopping_backend.agent_api.main:app --port 8000
# 浏览器打开 http://127.0.0.1:8000/chat
```

| # | 操作 | 期望 |
| --- | --- | --- |
| 1 | 点快捷句「推荐 2000 元以内的手机」 | 回答含 3 款；调试面板 4 个节点，无 `coupon_worker`；`degraded=false` |
| 2 | 同会话再问「第二款有货吗」 | `goods` 只有 1 条，且价格等于第 1 问的中间那款 |
| 3 | 把 token 改成错的 | 红色 `HTTP 401 UNAUTHORIZED`，回答区不崩 |
| 4 | 清空 session 输入框 | 422 + 逐条 errors 显示（页面不吞错误） |
| 5 | 问「帮我下单买第一个」 | 拒答话术；链路只有 `supervisor → summarize` |
| 6 | 只起 agent、不起 business，且 `BACKEND_DRIVER=http` | `degraded=true` + 降级话术，页面正常显示不白屏 |
| 7 | 中文输入法打「手机」二字回车确认候选 | **不发送**（F2-1 修复后） |
| 8 | 刷新页面 | 身份三字段还在（localStorage），历史按 F2-2 决定是否保留 |

---

## 九、风险与对策

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| 页面被误当成产品前端继续堆需求 | 偏离"跑通业务"主线 | 定位写死在本文件 §1 与页面底部说明：联调工具，正式界面归 MaxKB |
| 单文件膨胀难维护 | 改一处易出错 | 超过 ~600 行再拆（`chat.html` + `chat.js` + `chat.css` 三个静态文件，仍零构建）；触发条件写在这里，避免过早模块化 |
| token 存 localStorage | 本机泄露面极低，但若被部署到共享环境即风险 | 文档明示只用于本机；提供 `sessionStorage` 备选（F2-3 一并做）；不进版本库 |
| 静态资源随包发布失败（非 editable 安装） | `/chat` 500 | 已按"文件缺失 → 500 + 人话提示"处理，服务照常启动；`tests` 覆盖 editable 路径 |
| 前端与后端口径分叉（页面自己判断"有没有货"） | 两套规则，排障时互相矛盾 | 页面只做**展示**，任何条件判断来自 `data`/`meta` 字段；S 表已写死这条 |

---

## 十、v1 已落地清单（便于你直接验收）

```
新增  src/shopping_backend/web/chat.html        单文件页面（零依赖、零构建、同源 fetch）
改动  src/shopping_backend/agent_api/main.py    GET /chat（不进 OpenAPI）+ 索引字段 chat
改动  tests/test_agent_api.py                   2 条用例（页面可取 / 不污染对外契约）
```

验证结果：`uv run pytest tests/test_agent_api.py -q` → **19 passed**；`uv run ruff check src tests scripts alembic` → **All checks passed**。

用法（PowerShell）：

```powershell
cd D:\LLMProject\shopping\shopping-backend
uv run python -m uvicorn shopping_backend.agent_api.main:app --host 127.0.0.1 --port 8000
# 浏览器打开 http://127.0.0.1:8000/chat
```

要我接着做 v2 的话，建议从 **F2-1（输入法回车）+ F2-4（grounding 告警）+ F2-3（分支自检面板）** 起手：都不动后端，纯页面侧，风险最低，且把页面从"能问"升级成"能自证流程走对"。
