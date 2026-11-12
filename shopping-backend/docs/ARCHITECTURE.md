# 架构说明（ARCHITECTURE）

> 对应 `frame.md` 阶段 2。基座选型：**干净最小工程 + 沿用 template-langgraph 工程规范**（不整体复制模板），范式来源：**commerce-agents 的 Backend/Skill/Guardrail/Provenance 设计**。

---

## 一、分层与依赖方向

```
 ┌───────────────────────────────────────────────────────────────┐
 │ 入口层  agent_api (FastAPI :8000)   business_api (FastAPI :8001)│  ← HTTP / SSE
 ├───────────────────────────────────────────────────────────────┤
 │ 编排层  graph: builder / routing / supervisor / nodes          │  ← 只依赖 domain + backends 抽象
 ├───────────────────────────────────────────────────────────────┤
 │ 契约层  guardrails: validate_input / validate_output /         │  ← 所有跨边界数据的唯一收口
 │         fencing / grounding                                    │
 ├───────────────────────────────────────────────────────────────┤
 │ 适配层  backends: StorefrontBackend(ABC)                        │  ← inprocess / http / http_java
 ├───────────────────────────────────────────────────────────────┤
 │ 业务层  services: goods / stock / coupon / preference          │  ← 纯业务规则，无 HTTP 无框架
 ├───────────────────────────────────────────────────────────────┤
 │ 数据层  db: engine / session / base   domain: models_orm       │  ← 方言中立
 └───────────────────────────────────────────────────────────────┘
 横切：core(config, logging, errors, ids)  llm(registry, service)  seeds/  evals/
```

依赖方向严格自上而下；`services` 不 import `graph`，`backends` 不 import `fastapi`。这条约束让 M7 的 HITL / 新 Worker 只改编排层。

---

## 二、模块职责表

| 模块 | 职责 | 不做 |
| --- | --- | --- |
| `core/config.py` | `pydantic-settings` 读 `.env`；派生属性 `data_dir`、`is_sqlite`、`llm_enabled` | 不做业务判断 |
| `core/logging.py` | structlog；dev 彩色控制台、prod JSON；`trace_id` 绑定；JSONL 落 `logs/` | 不打印入参原文（脱敏后） |
| `core/errors.py` | `NotOffered` / `Unavailable` / `GuardrailViolation` / `Degraded`；异常 → 对外文案映射表 | 不把堆栈返回给调用方 |
| `db/engine.py` | **唯一方言分支处**：`create_engine_from_settings()`；SQLite 走 WAL + `busy_timeout` + `check_same_thread=False`，`:memory:` 走 `StaticPool`；MySQL/PG 走 `pool_pre_ping` + `pool_recycle` | 不写业务 SQL |
| `db/session.py` | `session_scope()` 上下文 + FastAPI 依赖；只读事务边界 | 不开启长事务 |
| `domain/models_orm.py` | SQLModel 表定义（goods/stock/coupon/user_preference/agent_request_log） | 不出现关系 ORM 的隐式懒加载（跨线程会炸） |
| `domain/schemas.py` | Pydantic DTO：`GoodsOut/StockOut/CouponOut/GoodsQuery/AgentRunIn/AgentRunOut/SupervisorDecision` | 不做校验逻辑（交给 guardrails 调用） |
| `domain/context.py` | `ShoppingSessionContext(user_id, session_id, tenant_id, trace_id, now)` | 不承载业务参数 |
| `backends/base.py` | ABC + 工厂 + `_OutputGuardMixin`（后置校验统一收口）+ 结果归一 | 不含具体协议 |
| `backends/inprocess.py` | 直连 services，`asyncio.to_thread` 包裹同步 DB 调用 | 不起 HTTP |
| `backends/http.py` | `httpx.AsyncClient` + 超时 + tenacity 重试 + `X-Internal-Token` + 状态码映射 | 不重试写操作（本阶段全只读） |
| `backends/http_java.py` | 同 http，额外一层**字段别名映射**（Java 命名 → 本工程 DTO） | 不改业务口径 |
| `guardrails/*` | 见 `docs/GUARDRAILS.md` | — |
| `graph/state.py` | `ShoppingAgentState`（Pydantic）+ checkpoint 安全存取助手 `get_goods()/set_goods()` | 不放业务规则 |
| `graph/routing.py` | 纯函数条件路由（可脱离图单测） | 不发起 IO |
| `graph/supervisor.py` | 意图 + 槽位；`rule_based` 与 `llm` 双实现，llm 非法枚举回落 rule | 不查数据 |
| `graph/nodes/*.py` | 3 个 Worker + summarize | 不互相直接调用（只经边） |
| `graph/nodes/presentation.py` | 话术模板渲染（O1–O9 落地）+ 金额/库存格式化 | 不调用 LLM |
| `graph/builder.py` | `StateGraph` 组装、`add_edge` / `add_conditional_edges`、`compile(checkpointer=...)` | 不含节点逻辑 |
| `graph/checkpoint.py` | `AsyncSqliteSaver`（默认）/ `InMemorySaver`（测试）/ Postgres（extra） | 不混用业务库 |
| `llm/service.py` | `wait_for` 超时 + 重试 + 多模型降级 + `call_structured()`；无 Key 时 `enabled=False` | 不做路由决策 |
| `agent_api/*` | 鉴权、入参校验、context 注入、图调用、异常→200 降级、响应组装 | 不写业务 |
| `business_api/*` | 三个只读接口 + admin seed + health | 不含 Agent 逻辑 |
| `seeds/loader.py` | JSON → SQLite；`reset=True` 先删后插 | 不参与运行期 |

---

## 三、LangGraph 图定义

### 3.1 节点与边

```python
# graph/builder.py 的目标形态
builder = StateGraph(ShoppingAgentState)
builder.add_node("supervisor",   supervisor_node)
builder.add_node("goods_worker", goods_worker_node)
builder.add_node("stock_worker", stock_worker_node)
builder.add_node("coupon_worker",coupon_worker_node)
builder.add_node("summarize",    summarize_node)

builder.set_entry_point("supervisor")

# 意图路由（代码硬路由，LLM 只影响 intent 的判定，不影响边）
# 注意：stock_query 也解析为 goods_worker —— 见 §3.3 / ADR A7
builder.add_conditional_edges("supervisor", route_from_supervisor, {
    "goods_worker":  "goods_worker",   # goods_query / composite / stock_query
    "coupon_worker": "coupon_worker",   # coupon_query
    "summarize":     "summarize",       # other
})
builder.add_conditional_edges(
    "goods_worker",
    route_after_goods,
    {"stock_worker": "stock_worker", "summarize": "summarize"},  # F1；无候选时不必再查库存
)
builder.add_conditional_edges(
    "stock_worker",
    route_after_stock,
    {"summarize": "summarize", "coupon_worker": "coupon_worker"},  # F2 / F3
)
builder.add_conditional_edges("coupon_worker", route_after_coupon, {"summarize": "summarize"})  # F4
builder.add_edge("summarize", END)
```

### 3.2 State 定义与序列化策略

```python
class ShoppingAgentState(BaseModel):
    # 输入
    user_id: str
    session_id: str
    tenant_id: str = "default"
    query: str
    trace_id: str = ""
    # 上下文
    preference_memory: dict[str, str] = Field(default_factory=dict)
    # 编排
    intent: Intent = "other"
    slots: dict[str, Any] = Field(default_factory=dict)      # 槽位（价格上限、品类、goods_ref…）
    coupon_needed: bool = False
    node_visits: dict[str, int] = Field(default_factory=dict) # F6 计数
    # 业务结果（★ 存 model_dump(mode="json") 后的 dict，不存模型对象）
    goods_list: list[dict[str, Any]] = Field(default_factory=list)
    stock_map: dict[str, dict[str, Any]] = Field(default_factory=dict)
    coupon_map: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)  # goods_id -> 可用券
    seen_goods_ids: list[str] = Field(default_factory=list)   # ★ 溯源白名单，用 list 不用 set
    # 输出与观测
    route_trace: list[str] = Field(default_factory=list)
    degraded: bool = False
    error_msg: str | None = None
    final_answer: str | None = None
```

三个刻意的选择（**M4 spike 必须验证通过**）：

| 选择 | 原因 |
| --- | --- |
| Worker 结果存 **dict** 而非 Pydantic 实例 | checkpointer 走 JSON/msgpack 序列化，模型对象跨版本反序列化不稳；读取时用 `GoodsOut.model_validate(d)` 即时还原，类型安全不丢 |
| `seen_goods_ids` 用 **list** 不用 set | `set` 不是 JSON 类型，恢复 checkpoint 后会变 list 导致类型漂移 |
| 不用 `Annotated[list, add_messages]` | 本服务不做自由对话历史（对话历史归 MaxKB），State 是结构化流程状态，两套会话体系分离（frame.md 阶段 3） |

### 3.3 为什么 `stock_query` 也经过 goods_worker

`frame.md` 原表写的是 `supervisor --stock_query--> stock_worker`。实现上收敛为：**库存分支必须先用一次窄口径搜索解析出真实 `goods_id`**，理由两条：

1. 用户说的是商品名（「红米 Note13 Pro」），库存接口只认 ID。**从名字直接拼 ID 就是编造**，违反溯源原则（commerce-agents 的 `seen_products` 白名单）。
2. 与 F1「商品 → 库存」的无条件边保持同一条链路，图里不需要第二条进 `stock_worker` 的路径，硬路由更简单可测。

因此 `route_from_supervisor` 对 `stock_query` 返回 `goods_worker`，并在 `slots.resolve_only=True` 上打标：`goods_worker` 只解析 ID（limit=3），`stock_worker` 完成后 `route_after_stock` 因 `resolve_only` 直接进 `summarize`，不再触发券分支。业务口径同步记在 `docs/SHOPPING_RULES.md` §3 与 §4。

### 3.4 一次请求的时序（`BACKEND_DRIVER=inprocess`）

```
MaxKB ──POST /api/shopping-agent/run──▶ agent_api.routes
   1. deps.verify_token(X-Internal / Bearer)      失败 → 401（不进图）
   2. guardrails.validate_input(AgentRunIn)       失败 → 422 友好文案
   3. services.preference.load_tier1(user_id)     → state.preference_memory
   4. build_context(user_id, session_id, trace_id)
   5. graph.ainvoke(state, config={"configurable":{"thread_id": session_id}})
        supervisor   ├ rule_based: 关键词+正则槽位
                     └ llm: llm_service.call_structured(SupervisorDecision) → 非法枚举回落 rule
        goods_worker └ services.goods.search()  → 后置护栏 → goods_list/seen_goods_ids
        stock_worker └ services.stock.query_available(ids) → stock_map
        [route_after_stock]  F2/F3 判定
        coupon_worker└ services.coupon.pick_available(ids) → coupon_map
        summarize    ├ presentation.render(state, reasons)
                     ├ (llm) 仅生成每条 reason
                     └ guardrails.grounding.verify(answer, state) → final_answer
   6. services.preference.extract_and_store(...)  后台任务，失败仅 WARN
   7. agent_request_log 落库（intent/route_trace/latency/degraded）
   8. 200 AgentRunOut（异常统一降级，见 §6）
```

`BACKEND_DRIVER=http` 时第 5 步内的三次 service 调用替换为 `httpx` 到 :8001 的三次 POST/GET，图与护栏逻辑完全相同——这正是「三 driver 同一份契约测试」要保证的等价性。

### 3.5 Checkpoint 与会话体系

| 体系 | 归属 | 存什么 | 存哪里 |
| --- | --- | --- | --- |
| 对话历史 | MaxKB | 用户与助手的消息、会话列表 | MaxKB 自有库（本仓库不碰） |
| 流程状态 | LangGraph checkpointer | State 快照（候选商品、库存、券、溯源 ID、route_trace） | `data/checkpoints.db`，`thread_id = session_id` |

- `thread_id` 命名空间：`f"{tenant_id}:{session_id}"`，为阶段 5 多租户与运营审核恢复留位（`GET /api/shopping-agent/state?session_id=` 可查快照，M5 只暴露内部方法）。
- 恢复语义：同 `session_id` 的第二次请求，先 `graph.aget_state(config)` 取 `values`，把上轮 `seen_goods_ids`/`goods_list` 作为候选上下文带入（用例 7）。
- 过期策略：SQLite 文件级清理脚本 `scripts/reset_db.py --checkpoints-days 30`（不做行级 TTL，M7 可加）。

---

## 四、LLM 接入与双模式

```
llm/registry.py  : OPENAI_API_KEY 为空 → registry.enabled == False
llm/service.py   : call_structured(schema, system, user)
                   ├ asyncio.wait_for(LLM_TIMEOUT_S)
                   ├ tenacity: 指数退避, MAX_LLM_CALL_RETRIES, 仅重试速率/超时/APIError
                   └ 多模型循环降级（LLM_MODEL → LLM_FALLBACK_MODEL）
supervisor.py    : llm 模式调用失败 / 返回枚举非法 → 记 degraded 并采用 rule_based 结果
summarize.py     : llm 模式只为 reason 文案；渲染骨架与所有数字仍来自 presentation.py
```

模式矩阵：

| `AGENT_MODE` | 需要 API Key | 意图判定 | 文案 | 图/边/护栏 | 用途 |
| --- | --- | --- | --- | --- | --- |
| `rule_based`（默认） | 否 | 关键词 + 正则槽位 | 纯模板 | **完全相同** | 本机跑通业务流程、CI、离线演示 |
| `llm` | 是 | 结构化输出（枚举校验） | 模板 + 模型 reason | **完全相同** | 接入真实模型后的生产形态 |

两种模式的差异被限制在「谁产出 intent 与 reason」，因此 `llm` 模式上线不引入新的路由风险；`FakeLLM`（固定返回 `SupervisorDecision`）在单测里覆盖 llm 分支。

---

## 五、依赖取舍

### 5.1 引入

| 包 | 用途 | 来自模板？ |
| --- | --- | --- |
| `fastapi` / `uvicorn[standard]` | 两个服务 | ✅ |
| `pydantic>=2` / `pydantic-settings` / `python-dotenv` | DTO + 配置（模板自己手写了非 pydantic-settings 的 Settings，此处用标准实现） | ✅（改良） |
| `langgraph>=1.0` / `langgraph-checkpoint-sqlite` / `aiosqlite` | 图编排 + 检查点 | ✅（换成 sqlite） |
| `langchain-core`（`llm` extra 才装 `langchain-openai`） | 消息与结构化输出 | ✅（收窄） |
| `sqlmodel` | 表定义 + 中立建表 | ✅ |
| `httpx` | http / http_java driver、TestClient | ✅ |
| `tenacity` | 后端与 LLM 重试 | ✅ |
| `structlog` | 结构化日志 | ✅ |
| `pytest` / `pytest-asyncio` / `ruff` | 测试与静态检查 | ✅（模板另有 pyright，作为可选） |

### 5.2 刻意不引入

`psycopg*`、`langgraph-checkpoint-postgres`（→ `postgres` extra）、`pymysql`（→ `mysql` extra）、`supabase`、`mem0ai` + pgvector（长期记忆改为自绘 `user_preference` 表 + `services/preference.py`）、`langfuse`（→ 保留 `LANGCHAIN_*` 环境变量透传，不装 SDK 也能跑）、`slowapi` + Redis、`prometheus-client` / Grafana / compose 监控栈（`deploy/` 里只留应用容器）、`passlib` + `jose` 完整用户体系（本阶段只做内部 token 校验）、`duckduckgo-search`、`uvloop`（Windows 不适用）。

剔除理由统一：**本机无 Docker/MySQL 环境下必须能 `uv sync` 成功并启动**，重依赖一律做成 extras 或文档化的「可后补平台能力」。

---

## 六、可观测与降级

| 手段 | 内容 |
| --- | --- |
| `route_trace` | 响应 `meta.route_trace` 返回走过的节点与选择的边，单测与 LangSmith 双重复核 |
| `agent_request_log` | trace_id / session / mode / backend_driver / intent / 商品数 / 券数 / latency_ms / error_code，用于回归与排障 |
| 结构化日志 | 每条 Worker 调用记录 `backend`、`rows_in/rows_kept/rows_dropped`、`timeout_ms`；WARN 只在护栏丢弃时出现 |
| LangSmith（透传） | config 里带 `tags=["shopping-agent", intent]` + `metadata`；设 `LANGCHAIN_TRACING_V2=true` 即接，不装额外 SDK |
| 降级矩阵 | 见下表；所有降级 HTTP 200 + `meta.degraded=true`，**不返回堆栈** |

| 故障 | 检测点 | 行为 |
| --- | --- | --- |
| 业务后端超时/5xx | `backends/http.py` | 该域数据置空 + `error_msg`，继续到 summarize → 降级话术（O8） |
| 返回值 Schema 非法 | `validate_output` | 逐条丢弃；整批非法 → 空 + `degraded` |
| 回答含未溯源 ID/价格 | `grounding` | 剔除所在行；全部剔除 → 回退纯模板文案 |
| LLM 超时/非法枚举 | `supervisor` | 回落 rule_based 意图，`degraded=true` |
| checkpointer 不可用 | `graph/checkpoint.py` | **启动即失败**（对齐模板：检查点不做静默退化），日志明确指向 `CHECKPOINT_DB_PATH` |
| API Key 缺失但 `AGENT_MODE=llm` | `core/config.llm_enabled` | 启动 WARN + 自动按 `rule_based` 执行（不崩，便于灰度） |

---

## 七、测试策略

| 文件 | 覆盖 | 关键点 |
| --- | --- | --- |
| `test_db_engine.py` | engine 工厂 | SQLite 建库/PRAGMA；断言所有列类型方言中立（无 `JSON`/`AUTOINCREMENT`/`Float` 金额） |
| `test_services.py` | 业务规则 | R-S1/R-S2、券六步判定、paused/expired/抢完过滤、percent 折算 |
| `test_backends.py` | **契约等价** | 同一份断言表参数化跑 `inprocess/http/http_java` 三实现（`httpx.MockTransport` 造响应） |
| `test_guardrails.py` | 护栏 | 未知字段丢弃、非法行不入 State、fence 去角色伪造、grounding 拒绝编造 ID/价格 |
| `test_routing.py` | 硬路由 | F2/F3 两条分支 + resolve_only + node_visits 上限，**纯函数不建图** |
| `test_graph_e2e.py` | 全链路 | `rule_based` + `InMemorySaver` + `FakeBackend`，断言 route_trace 与 final_answer 溯源 |
| `test_checkpoint.py` | 恢复 | 两次 ainvoke 同 thread_id，第二次能见 `seen_goods_ids` |
| `test_api.py` | 服务 | 401 / 422 / 200 降级 / health；TestClient + 内存 SQLite |

约定：CI 里**不需要任何外网与模型 Key**（`pytest` 全绿即代表 G2/G3/G5/G6 达成）。

---

## 八、关键决策记录（ADR）

| # | 决策 | 备选 | 理由 |
| --- | --- | --- | --- |
| A1 | 新建干净最小工程，沿用模板规范 | 整体复制 template-langgraph 做减法 | 模板 Postgres/mem0/langfuse/Redis 全套耦合，本机装不起来且减法成本高；规范（lifespan/schemas/services/structlog）照抄即可 |
| A2 | 业务后端在**同仓库**内用 FastAPI Mock 实现 | 只做 Agent，业务数据内联 | 后续替换 Java 后端时契约已跑通；`BACKEND_DRIVER` 一级切换验证真实 HTTP 拓扑 |
| A3 | SQLite 双库分离（business / checkpoints） | 单文件 | 业务库要切 MySQL，检查点要留 SQLite 或切 PG，耦合在一起切换脚本无法分离 |
| A4 | `services` 用同步 SQLAlchemy + `asyncio.to_thread` | 全异步 `async_sessionmaker` | 业务层可被两个服务与测试同步复用；避免异步 ORM 在 SQLite 上的连接共享坑；将来换 PG 只需把 engine 换成 async engine |
| A5 | State 存 dict，DTO 在边界还原 | 直接存 Pydantic 对象 | 检查点序列化稳定（§3.2） |
| A6 | 话术由模板渲染，模型只写 `reason` | 让模型自由生成整段回答 | 从根本上满足 O2/O9 防幻觉；与 commerce-agents「展示 payload 只允许服务端字段」一致 |
| A7 | 库存分支先解析商品 ID（不直连 stock） | 按 frame.md 原表直连 | §3.3；避免名称→ID 编造 |
| A8 | 默认 `rule_based` | 默认 `llm` | 核心目标是「先跑通业务流程」，不能被 Key 阻塞；llm 为配置项 |
| A9 | 内部静态 token 鉴权 | 复用模板 JWT 全体系 | MaxKB 外部工具是服务间调用；JWT 属 MaxKB 职责（frame.md：鉴权边界） |
| A10 | `alembic` 保留但默认 `create_all` | 只用 create_all / 只用 alembic | SQLite 开发期 create_all 省事；切 MySQL 时迁移是刚需，脚本与 `env.py` 提前留好 |

---

## 九、与 frame.md 的对应关系

| frame.md 阶段 | 本仓库位置 | 状态 |
| --- | --- | --- |
| 阶段 1：干净 LangGraph 工程 + 导购业务规范 | `README.md`、`docs/SHOPPING_RULES.md`、M0 | ✅ |
| 阶段 1：Docker 部署 MaxKB | 仓库外 | 不做（本机无 Docker），`docs/MAXKB_INTEGRATION.md` 给配置指引 |
| 阶段 2：State / Adapter / Skill+Guardrail / 3 Worker / 混合路由 / 汇总 / FastAPI 接口 / LangSmith+Checkpoint | M1–M5，`docs/API.md` | ✅ 222 测试全绿 |
| 阶段 3：MaxKB↔LangGraph 集成、异常适配、记忆打通 | `docs/MAXKB_INTEGRATION.md` + M5 服务侧 | 服务侧就绪（含降级与偏好回写），MaxKB 侧人工配置 |
| 阶段 4：单测 / 链路核验 / 灰度 / Docker 打包 | M6（测试 + `scripts/run_demo.py`）、M7（Docker 构建） | 单元+端到端 7/7 通过；容器资产预置不构建 |
| 阶段 5：HITL、运营审核恢复、新 Worker、评估 | `docs/SHOPPING_RULES.md` §8 + M7 | 留位 |

---

## 十、实现期发现（M0–M6 期间对设计的修正）

方案文档写于代码之前，落地时有 12 处与实际库行为或本机环境冲突。这些不是补丁说明，而是后续维护必须知道的约束，因此集中记录在此。

| # | 发现 | 原方案 | 落地处置 | 影响面 |
| --- | --- | --- | --- | --- |
| D1 | checkpoint 状态在同 `thread_id` 上是**累加合并**的 | 假定每轮 State 独立 | `supervisor_node` 显式合并 `PER_TURN_RESET`（`graph/state.py`）：工作集每轮清零，`seen_goods_ids`/`candidate_memory` 有意跨轮保留 | State 设计、用例 7 断言 `route_trace` 不串轮 |
| D2 | 带 checkpointer 的图**必须**传 `thread_id`，否则 `ValueError` | 未标注 | `graph_config()` 统一生成 `thread_id = tenant:session`；`test_checkpoint.py` 把该约束钉成用例 | API 层每次调用必须带 session_id |
| D3 | SQLModel 0.0.48 默认列类型是 `UTCDateTime`，**拒绝 naive datetime** | 方案 §4.5「naive UTC 存库」 | 全链路改为 timezone-aware UTC（`domain/context.utcnow()` + `ensure_utc()` 归一口径） | 时间口径、种子 JSON 解析、券有效期比较 |
| D4 | SQLModel 字符串列实际类型是 `AutoString`、PK `autoincrement="auto"` | 单测用 `isinstance(col.type, sa.String)` 断言 | 类型断言改为**编译后 DDL**（`_rendered()` 对 mysql/postgres/sqlite 三方言各渲染一次），断言的是"建表会生成什么" | `test_db_engine.py` 中立性检查更严格 |
| D5 | `str(SomeStrEnum)` 在 3.13 返回 `"GoodsStatus.ON_SALE"` 而非值 | 代码里以 `str(enum)` 落库/建索引表 | 统一用 `.value` 落库，比较用枚举成员；`model_dump(mode="json")` 天然输出值 | 修复了偏好写库、券 scope/discount 映射两处真实 bug |
| D6 | `expire_on_commit` 默认 True 导致 service 返回的 ORM 行在会话关闭后不可读（`DetachedInstanceError`） | 未预见 | `session_scope` 统一 `expire_on_commit=False`（读路径把行当 DTO 用，是 A4 同步业务层的必然结果） | `db/session.py` |
| D7 | `httpx` 的 timeout 属于 client；注入的 client 会绕过 `BUSINESS_API_TIMEOUT_S`；headers 同理 | 只在自建 client 上设置 | timeout 与 `X-Internal-Token` 改为**每请求下发**，保证 env 配置在任何 transport 下都生效 | `backends/http.py`，`test_backends.py` 新增两条钉桩用例 |
| D8 | `ASGITransport` 不执行 httpx 超时 | demo 用例 5 想用例内超时模拟 | 用例改为：断言 timeout 确实落到 `request.extensions["timeout"]`，再由自定义 transport 抛 `ReadTimeout` 验证降级；真实超时由 demo 的第 3 个进程（打不通的 9 端口）覆盖 | 测试有效性，非产品缺陷 |
| D9 | 优惠券的 `display` 文案若来自 wire，Java 侧漏传即空白 | 由 service 生成、driver 透传 | `StorefrontBackend` 在校验后**统一重算** `display`（派生数据不接受外部输入） | 三驱动输出完全一致 |
| D10 | 券的按批校验后回连 goods_id 时，用 wire 字段名查不到（Java 用 `couponCode`） | 以 code 反查映射 | 改为**分桶校验**（per-bucket validate），身份只认"来自哪个桶" | `backends/base.get_coupon` |
| D11 | 库存分支若直连 `stock_worker`，就必须把商品名当 ID | `frame.md` 原表：`stock_query → stock_worker` | 改为 `stock_query → goods_worker(resolve_only)`，ID 一律来自搜索结果（ADR A7） | 路由表、用例 2 |
| D12 | 本机无 `make` | README/Makefile 以 make 为入口 | 主入口改为 `scripts/`（`init_db.py`、`run_demo.py`、`run_dev.sh/.ps1`、`demo_curl.sh`），`Makefile` 保留给 Linux/CI | 文档所有命令示例 |

另外两处是**质量修正**而非约束：`graph/supervisor.py` 的规则引擎在实测中暴露出 4 个缺陷（`不要超过X` 被同时解析成上下限、`有哪些`/`的电视` 等虚词污染商品指代、`1000到2000元` 区间不解析、`帮我下单` 未拒答），已修复并各补一条用例钉住（`test_supervisor.py` 41 条）。`presentation.py` 的话术模板按实测输出改了三处：`款` 与条件之间的空格规则、同券多商品合并成一行（避免逐条重复）、以及「¥ 前缀=来自 State 的事实、元 前缀=复述用户原话」这条让 grounding 不误伤澄清话术的记法约定。
