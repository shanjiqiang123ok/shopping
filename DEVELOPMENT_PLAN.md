# shopping-backend 开发方案与实施计划

> 依据 `frame.md`（MaxKB + template-langgraph + commerce-agents 组合实施方案）落地
> 本仓库定位：**独立的 LangGraph 导购 Agent 服务 + 内置 Mock 业务后端**
> 本地约束：**不装 Docker、不装 MySQL**，先用 SQLite 跑通完整业务流程，扩展逻辑预先留好
> 生成时间：2026-11-11

---

## 一、目标与非目标

### 1.1 本阶段目标（必须达成）

| 编号 | 目标 | 验收标准 |
| --- | --- | --- |
| G1 | 一条命令在本地启动两个服务（Agent 服务 8000、业务后端 8001），无需 Docker、无需 MySQL | `make dev` / `scripts/run_dev.sh` 启动成功，`/health` 均返回 200 |
| G2 | 用 SQLite 跑通「提问 → 商品 → 库存 → 优惠券 → 汇总回答」全链路 | `curl POST /api/shopping-agent/run` 返回带商品/库存/优惠券的结构化回答 |
| G3 | Supervisor + 3 个 Worker + 代码硬路由（ConditionalEdge）按 frame.md 规则分支 | 库存>0 直接汇总；库存=0 走优惠券；路由轨迹 `route_trace` 可断言 |
| G4 | BackendAdapter 抽象层，业务数据源可插拔（进程内 / HTTP / 未来 Java） | 改一个环境变量 `BACKEND_DRIVER` 即在 inprocess 与 http 之间切换，图代码零改动 |
| G5 | 双层 Guardrail：入参 Schema 校验 + 返回值 Schema 校验 + 防幻觉落地校验 | 单测覆盖：伪造字段被丢弃、非法商品 ID 被拦截、越权/编造价格被拒 |
| G6 | Checkpoint 持久化（SQLite），同一 `session_id` 可恢复流程状态 | 同一 session 连续两次提问，第二次能引用第一次的候选商品 |
| G7 | 无 API Key 也能端到端演示（rule_based 模式），有 Key 时切 llm 模式 | `AGENT_MODE=rule_based` 零外网调用完成全链路 |
| G8 | 数据库/容器化的切换路径写清并可验证（迁移脚本 + 文档 + 可选 Dockerfile） | 改 `DATABASE_URL` 为 MySQL/PG 即可运行；`docker compose up` 配置就绪（本机不执行） |

### 1.2 明确不做（本阶段）

- ❌ 不修改 MaxKB 源码，不在本仓库内部署 MaxKB（仅在文档中给出外部工具配置样例）
- ❌ 不直接引入 `commerce-agents` 源码（原生绑定 Anthropic SDK），只移植其**设计范式**
- ❌ 不部署 Docker、不安装 MySQL、不配置 pgvector / Redis / Prometheus / Grafana
- ❌ 不做 human-in-the-loop 人工断点、不做异步流式（frame.md 阶段 5，本阶段仅留接口位）
- ❌ 不做多租户隔离与完整用户体系（仅透传 `user_id` / `tenant_id` / token 校验）

---

## 二、整体架构

```
用户 / 运营
   ↓（frame.md 阶段3，本仓库外）
MaxKB（聊天界面、鉴权、会话、RAG、模型网关、外部 HTTP 工具）
   ↓ HTTP  POST /api/shopping-agent/run   {user_id, session_id, query, token}
┌──────────────────────────────────────────────────────────────┐
│ shopping-backend :: Agent 服务 (FastAPI :8000)               │
│                                                              │
│  FastAPI 层     鉴权校验 / 入参 Schema / 限流位 / 统一异常文案 │
│  Graph 层       Supervisor ─硬路由─▶ Worker(goods/stock/coupon)│
│                 │ ConditionalEdge 代码兜底 + LLM 意图调度      │
│                 └─▶ Summarize（只读校验后的 state 生成回答）    │
│  Guardrail 层   前置入参校验 / 后置返回值校验 / 溯源(provenance)│
│  Adapter 层     StorefrontBackend (ABC)                       │
│                 ├ InProcessBackend   ← 默认，直连 service 层   │
│                 ├ HttpBackend        ← 调本机 Mock 业务 API    │
│                 └ HttpJavaBackend    ← frame.md 最终目标(留位) │
│  持久化层       SQLite checkpointer (data/checkpoints.db)      │
│                 偏好记忆 / 请求日志                            │
└──────────────────────────────────────────────────────────────┘
   ↓ BACKEND_DRIVER=http 时走 HTTP（127.0.0.1:8001）
┌──────────────────────────────────────────────────────────────┐
│ shopping-backend :: 业务后端服务 (FastAPI :8001)              │
│   GET /api/v1/goods/search   POST /api/v1/stock/query         │
│   POST /api/v1/coupon/query  GET /api/v1/goods/{id}           │
│   Service 层（业务规则：可售、券门槛、叠加）                    │
│   SQLAlchemy Core/ORM ── DATABASE_URL（默认 SQLite）           │
│   data/business.db  ← 表结构方言中立，可切 MySQL/PostgreSQL    │
└──────────────────────────────────────────────────────────────┘
```

关键取舍：

1. **两个业务库文件分离**：`business.db`（业务数据，将来切 MySQL）与 `checkpoints.db`（LangGraph 流程快照，将来切 Postgres），避免两类切换互相耦合。
2. **业务后端默认走 `inprocess`**：Agent 直接调用同仓库的 Service 层函数，本地跑流程零网络依赖、零端口冲突；但代码路径与 HTTP 路径共用同一套 DTO 与 Guardrail，因此切到 `http` / `http_java` 时行为一致。
3. **双执行模式**：`rule_based` 用关键词/规则做意图判断（不依赖大模型，保证 G2 一定可验收）；`llm` 模式接 OpenAI 兼容端点做开放意图调度。图结构、路由边、Guardrail 在两种模式下完全相同。

---

## 三、目录结构

```
D:\LLMProject\shopping\shopping-backend\
├── pyproject.toml              # uv 管理，requires-python >=3.13，src layout
├── README.md                   # 3 分钟跑通指南
├── Makefile                    # bootstrap/dev/dev-http/test/lint/seed/eval
├── .env.example                # 全部配置项 + 注释（不含真实密钥）
├── .gitignore  .dockerignore  .python-version
├── alembic/                    # 方言中立的迁移（SQLite 可用，切 MySQL 时用）
│   ├── env.py                  # DATABASE_URL 从 Settings 注入，非硬编码
│   └── versions/0001_init.py
├── src/shopping_backend/
│   ├── __init__.py
│   ├── core/
│   │   ├── config.py           # pydantic-settings：DB / LLM / 鉴权 / 模式
│   │   ├── logging.py          # structlog，JSONL 文件 + 控制台
│   │   ├── errors.py           # NotOffered / Unavailable / GuardrailViolation / 对外文案映射
│   │   └── ids.py              # trace_id / request_id
│   ├── db/
│   │   ├── engine.py           # ★ create_engine_from_settings()，方言分支集中在此
│   │   ├── session.py          # session factory / 依赖注入 / 事务边界
│   │   └── base.py             # DeclarativeBase，统一命名约定
│   ├── domain/
│   │   ├── models_orm.py       # goods / stock / coupon / user_preference 表
│   │   ├── schemas.py          # ★ Pydantic 出参契约（Guardrail 后置校验依据）
│   │   ├── context.py          # ShoppingSessionContext(user_id, session_id, tenant_id, trace_id)
│   │   └── enums.py            # DiscountType / GoodsStatus / Intent ...
│   ├── backends/
│   │   ├── base.py             # ★ StorefrontBackend ABC + 工厂
│   │   ├── inprocess.py        # 直连 service 层
│   │   ├── http.py             # httpx + 超时/重试/鉴权/异常归一
│   │   └── http_java.py        # Java 后端字段映射适配（留位，接口已定义）
│   ├── guardrails/
│   │   ├── validate_input.py   # 前置：Agent 入参 + 工具入参 Schema 校验
│   │   ├── validate_output.py  # 后置：返回值逐条校验，非法字段丢弃/告警
│   │   ├── fencing.py          # 外部文本清洗（控制字符、角色伪造、截断）
│   │   └── grounding.py        # 防幻觉：回答中的商品ID/价格必须在 state 里可溯源
│   ├── graph/
│   │   ├── state.py            # ★ ShoppingAgentState（Pydantic）
│   │   ├── routing.py          # ★ 硬路由函数（ConditionalEdge 条件）
│   │   ├── supervisor.py       # 意图判定：rule_based / llm 双实现
│   │   ├── builder.py          # StateGraph 组装与 compile
│   │   ├── checkpoint.py       # ★ checkpointer 工厂（sqlite / postgres / mysql 留位）
│   │   └── nodes/
│   │       ├── goods_worker.py
│   │       ├── stock_worker.py
│   │       ├── coupon_worker.py
│   │       ├── summarize.py
│   │       └── presentation.py   # ★ 话术模板渲染（O1–O9），数字只来自 State
│   ├── llm/
│   │   ├── registry.py         # 模型清单 + 降级顺序（无 Key 时为空）
│   │   └── service.py          # 超时 + tenacity 重试 + structured_output
│   ├── services/               # ★ 业务规则层（Agent 与业务 API 共用）
│   │   ├── goods.py            # 搜索/筛选/详情
│   │   ├── stock.py            # 可售库存 = on_hand - reserved，多仓汇总
│   │   ├── coupon.py           # 门槛/适用范围/有效期/券库存
│   │   └── preference.py       # 偏好读写 + 敏感信息写入过滤
│   ├── agent_api/
│   │   ├── main.py             # FastAPI lifespan：预热图、建表/迁移、关连接
│   │   ├── deps.py             # token 校验、context 注入、backend 注入
│   │   └── routes.py           # POST /api/shopping-agent/run、GET /health
│   └── business_api/
│       ├── main.py
│       └── routes.py           # goods/stock/coupon/admin-seed
├── seeds/
│   ├── goods.json  stock.json  coupons.json  # 演示数据（手机/耳机/配件等）
│   └── loader.py
├── tests/
│   ├── conftest.py             # 内存 SQLite fixture、FakeBackend、FakeLLM
│   ├── test_db_engine.py       # SQLite 建库/方言中立性
│   ├── test_services.py        # 库存/券业务规则
│   ├── test_backends.py        # 三个 backend 行为一致（同一断言表跑三遍）
│   ├── test_guardrails.py      # 入参/出参/防幻觉
│   ├── test_routing.py         # 硬路由分支（库存0/大于0）★核心
│   ├── test_graph_e2e.py       # 全链路（rule_based 模式，无外网）
│   └── test_api.py             # 两个服务的 TestClient 用例
├── scripts/
│   ├── run_dev.sh  run_dev.ps1 # 双进程启动（无 Docker）
│   ├── init_db.py  reset_db.py
│   ├── demo_curl.sh            # 端到端演示脚本（可直接贴给后端/PM 看）
│   └── switch_db_example.py    # SQLite → MySQL 的校验脚本
├── deploy/                     # ★ 留好但本机不使用
│   ├── Dockerfile              # 多阶段，uv 安装
│   ├── docker-compose.yml      # agent + business + mysql + healthcheck
│   └── mysql.cnf.example
└── docs/
    ├── ARCHITECTURE.md         # 图/路由/state/时序详解
    ├── SHOPPING_RULES.md       # ★ frame.md 阶段1 交付物：导购业务规范文档
    ├── API.md                  # 两个服务的 OpenAPI 说明 + curl 示例
    ├── EXTENSION_DB_DOCKER.md  # ★ 切 MySQL/PG、上 Docker 的完整步骤
    ├── GUARDRAILS.md           # 护栏规则清单（对齐 commerce-agents docs/safety.md）
    └── MAXKB_INTEGRATION.md    # 外部 HTTP 工具配置、异常文案、记忆打通（阶段3）
```

---

## 四、核心设计

### 4.1 State（Pydantic，对齐 frame.md 阶段 2）

```python
class ShoppingAgentState(BaseModel):
    # 输入
    user_id: str
    session_id: str
    tenant_id: str = "default"
    query: str
    trace_id: str

    # 上下文
    preference_memory: dict[str, str] = {}

    # 中间结果（均由 Worker 写入，且必须是"已通过后置护栏"的数据）
    intent: Intent                          # goods_query|stock_query|coupon_query|composite|other
    goods_list: list[GoodsOut] = []         # 结构化，非自由文本 → 天然防编造
    stock_map: dict[str, StockOut] = {}
    coupon_map: dict[str, list[CouponOut]] = {}
    seen_goods_ids: set[str] = set()        # ★ 溯源记录（commerce-agents provenance）

    # 可观测 & 输出
    route_trace: list[str] = []             # 每步走了哪个节点、走了哪条边
    error_msg: str | None = None            # 对外友好文案，不含堆栈
    final_answer: str | None = None
```

设计要点：
- Worker 只写**结构化对象**，LLM 不参与数据构造，只参与意图判断与文案组织。
- `seen_goods_ids` 是溯源白名单：`summarize` 产出的回答里出现的任何商品 ID / 价格，都会被 `grounding.py` 与 state 对账，不一致则拒绝输出该片段。
- `route_trace` 让「代码硬路由」可被单测断言、被 LangSmith/日志复核。

### 4.2 路由（frame.md 的硬路由规则 1:1 落地）

| 当前节点 | 条件 | 下一节点 | 实现方式 |
| --- | --- | --- | --- |
| `supervisor` | intent=composite / goods_query | `goods_worker` | ConditionalEdge |
| `supervisor` | intent=stock_query | `goods_worker`（`slots.resolve_only=True`，仅用于解析商品 ID） | ConditionalEdge |
| `supervisor` | intent=coupon_query | `coupon_worker` | ConditionalEdge |
| `supervisor` | intent=other | `summarize`（澄清回答） | ConditionalEdge |
| `goods_worker` | 恒定（硬编码兜底） | `stock_worker` | `add_edge` 无条件边 |
| `stock_worker` | 存在 `available > 0` | `summarize` | ConditionalEdge |
| `stock_worker` | 全部 `available == 0` | `coupon_worker` | ConditionalEdge |
| `coupon_worker` | 恒定 | `summarize` | `add_edge` |
| `summarize` | 恒定 | `END` | `add_edge` |

- 核心业务顺序（商品→库存→券）由**代码兜底**，不依赖 LLM 记忆（frame.md 关键约束）。
- `llm` 模式下 LLM 只做 `intent` 分类与开放意图补充（结构化输出 → `Intent` 枚举校验，非法枚举退回 rule_based 结果）。
- **库存分支不直连 `stock_worker`**：用户说的是商品名，库存接口只认 ID，从名字直接拼 ID 即编造。故 `stock_query` 先经一次窄口径搜索（`limit=3`）解析出真实 `goods_id` 并写入 `seen_goods_ids`，再由 `resolve_only` 标记使 `stock_worker` 之后直接进 `summarize`，不触发券分支。理由与取舍见 `shopping-backend/docs/ARCHITECTURE.md` §3.3 与 ADR A7。

### 4.3 BackendAdapter（ABC，对齐 commerce-agents `StorefrontBackend`）

```python
class StorefrontBackend(ABC):
    @abstractmethod
    async def search_goods(self, ctx: ShoppingSessionContext,
                           query: GoodsQuery) -> list[GoodsOut]: ...
    @abstractmethod
    async def get_stock(self, ctx: ShoppingSessionContext,
                        goods_ids: list[str]) -> dict[str, StockOut]: ...
    @abstractmethod
    async def get_coupon(self, ctx: ShoppingSessionContext,
                         goods_ids: list[str]) -> dict[str, list[CouponOut]]: ...
```

约定（全部来自 commerce-agents 范式）：
- **身份只从 `ctx` 取**，工具入参里永远不出现 `user_id`，避免模型伪造越权。
- 领域异常两态：`NotOffered`（该业务不支持，原样转述非错误）/ `Unavailable`（暂时不可用，只报 ID 不报堆栈）。
- **Adapter 永不向上抛异常**：任何失败归一为 `{items|map, error_msg}`，保证「工具失败不终结本轮对话」。
- HTTP 实现统一封装：`timeout`（默认 5s）、`tenacity` 重试（幂等 GET 才重试）、`X-Internal-Token` 鉴权、错误码映射。
- 工厂 `build_backend(settings)` 按 `BACKEND_DRIVER=inprocess|http|http_java` 返回实例；三者通过**同一份契约测试**（`test_backends.py` 参数化跑三遍）。

### 4.4 双层 Guardrail

| 层 | 位置 | 规则 | 失败动作 |
| --- | --- | --- | --- |
| 前置 | Agent API 入参 | Pydantic `AgentRunIn`：query 长度 1–2000、user_id/session_id 非空且字符白名单 | 422，友好文案 |
| 前置 | 工具/Worker 入参 | `GoodsQuery` 等 `extra="forbid"`，limit 上限 20、价格区间非负 | 返回"参数非法"文本，不抛异常 |
| 后置 | Backend 返回值 | 逐条 `GoodsOut/StockOut/CouponOut.model_validate`，非法字段丢弃，整批非法则置空 + `error_msg` | 丢弃 + WARN 日志 + 计数 |
| 后置 | 字段白名单 | 只允许 schema 声明字段进入 state（`model_dump` 重新序列化） | 未知字段一律不落 state |
| 溯源 | `add_to_cart`/推荐类动作（留位） | ID 必须在 `seen_goods_ids` | 拒绝执行 |
| 防幻觉 | `summarize` 输出 | 回答中的商品 ID / 价格 / 券码必须在 state 中可查到 | 剔除该片段；全部剔除则回退模板文案 |
| 文本清洗 | 所有外部文本入 prompt | 去控制字符/双向字符、伪造 `system:`/`assistant:` 角色、截断 8000 字符 | 归一化后使用 |

### 4.5 数据模型（方言中立，SQLite 先跑通）

```sql
-- business 库
goods(id PK, sku, title, category, brand, description,
      price_cents INTEGER, currency CHAR(3) DEFAULT 'CNY',
      tags TEXT /*JSON*/, status VARCHAR16 /*on_sale|off_sale|draft*/,
      created_at DATETIME, updated_at DATETIME)

stock(id PK, goods_id FK, warehouse VARCHAR32,
      on_hand INTEGER, reserved INTEGER,
      updated_at DATETIME, UNIQUE(goods_id, warehouse))

coupon(id PK, code UNIQUE, name,
       scope VARCHAR16 /*all|category|goods*/, scope_value TEXT,
       threshold_cents INTEGER, discount_type VARCHAR16 /*percent|amount|shipping*/,
       discount_value INTEGER, valid_from DATETIME, valid_to DATETIME,
       total_quota INTEGER, used_count INTEGER, per_user_limit INTEGER,
       status VARCHAR16 /*active|paused|expired*/)

user_preference(user_id, pref_key, pref_value, category /*preference|constraint|context*/,
                source_session_id, updated_at, PK(user_id, pref_key))

agent_request_log(id PK, trace_id, session_id, user_id, mode, backend_driver,
                  intent, route_trace TEXT, goods_n, stock_n, coupon_n,
                  latency_ms, error_code, created_at)   -- 观测/回归用
```

中立性要求（写进单测 `test_db_engine.py`）：
- 主键用 `String(36)`（UUID 字符串）而非 SQLite 自增 / MySQL AUTO_INCREMENT，保证三种方言同一套迁移。
- 金额一律 `*_cents: Integer`，禁止 Float。
- 时间用 `DateTime`（UTC 存储，展示层转换）。
- JSON 字段用 `Text` 手动 `json.dumps/loads`，不用方言专属 `JSON` 类型。
- 所有查询走 SQLAlchemy Expression / ORM，不写裸方言语句（如 `ON DUPLICATE KEY` 用「先 SELECT 再 INSERT/UPDATE」或 SQLAlchemy 通用 upsert helper）。

### 4.6 切换与扩展逻辑（无 Docker / 无 MySQL 也能预留）

**DB 切换**（唯一改动点集中在 `core/config.py` + `db/engine.py`）：

| 项 | SQLite（默认，本机） | MySQL（后续） | PostgreSQL |
| --- | --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./data/business.db` | `mysql+pymysql://user:pw@host:3306/shopping` | `postgresql+psycopg://...` |
| 驱动依赖 | 内置 | `pymysql`（extra: `mysql`） | `psycopg[binary]`（extra: `postgres`） |
| engine 差异 | `check_same_thread=False` + WAL + `StaticPool`（内存测试） | `pool_size/max_overflow/pool_recycle=3600` | 同 MySQL |
| 建表 | `SQLModel.metadata.create_all` 或 `alembic upgrade head` | `alembic upgrade head`（推荐） | `alembic upgrade head` |
| checkpointer | `AsyncSqliteSaver(checkpoints.db)` | 保留 SQLite 或换 `PostgresSaver` | `AsyncPostgresSaver`（frame.md 原始形态） |

`engine.py` 内以 `match url.get_backend_name()` 分支集中处理 `connect_args` / `poolclass` / 是否启用 `sqlmode_silent`，业务代码零感知。生产切换文档：`docs/EXTENSION_DB_DOCKER.md`。

**Docker 支持**：`deploy/` 目录预置 `Dockerfile`（uv 多阶段 + 非 root + HEALTHCHECK）与 `docker-compose.yml`（agent + business + mysql8 + healthcheck + `depends_on: condition: service_healthy`）。本机不构建，但 `Makefile` 里 `make docker-build` / `make docker-up` 目标已写好，后续零改造。compose 通过环境变量把 `DATABASE_URL` 指向 mysql 服务，代码无需再改。

**backend 切换**：`BACKEND_DRIVER=inprocess → http → http_java` 三级；`http_java` 已实现为「路径映射 + 字段别名映射」两层（`PATHS` / `ALIASES` 两张表），对接真实 Java 时只改这两张表的值，无需动图与护栏；契约由 `tests/test_backends.py` 用 `httpx.MockTransport` 造 Java 风格响应验证，不依赖 Java 服务在线。详见 `shopping-backend/docs/EXTENSION_DB_DOCKER.md` §4。

### 4.7 LLM 与 Checkpoint

- `llm/registry.py`：无 `OPENAI_API_KEY` 时注册表为空 → `AGENT_MODE=rule_based` 强制生效，服务正常启动（**不报错**，只在启动日志打 WARN）。
- `llm/service.py`：`asyncio.wait_for(timeout)` + `tenacity` 指数退避重试 + 多模型循环降级 + `with_structured_output(SupervisorDecision)`。
- `graph/checkpoint.py`：默认 `AsyncSqliteSaver`（`aiosqlite`，WAL 模式，单进程内共享连接）；提供 `InMemorySaver` 用于单测。
- LangSmith：只保留 `LANGCHAIN_TRACING_V2` / `LANGSMITH_PROJECT` 环境变量与 config 透传（`tags=["shopping-agent"]`），本机默认关闭，接 LangSmith 时改 env 即可。

---

## 五、接口契约

### 5.1 Agent 服务（供 MaxKB 外部工具调用）

```
POST /api/shopping-agent/run
Header: Authorization: Bearer <AGENT_INTERNAL_TOKEN>   # 校验透传 token，非法直接 401
Body:
{
  "user_id": "u_1001",
  "session_id": "s_2026_11_11_001",
  "tenant_id": "default",
  "query": "帮我推荐 2000 元以内、有现货的手机，有券吗",
  "preference_memory": {}        # 可选，MaxKB 侧偏好摘要回传
}
200 Response:
{
  "request_id": "...", "trace_id": "...",
  "answer": "为您找到 3 款 2000 元以内的在售手机：\n1) Redmi Note 13 Pro ¥1699（现货 12 件）…\n可用券：满1500减100（券码 M1500_100）",
  "data": {
    "goods": [{"id":"g_001","title":"...","price_cents":169900,"available":12}],
    "coupons": [{"code":"M1500_100","threshold_cents":150000,"discount_value":10000}]
  },
  "meta": {"intent":"composite","mode":"rule_based","backend":"inprocess",
           "route_trace":["supervisor","goods_worker","stock_worker","coupon_worker","summarize"],
           "latency_ms": 42, "degraded": false}
}
```

- 错误：所有异常（超时/后端不可用/护栏拦截）统一 `200 + answer="暂时无法获取商品信息，请稍后再试" + meta.degraded=true`，**不返回堆栈**（frame.md 阶段 3「异常适配」要求）；仅鉴权失败 401、参数非法 422。
- `GET /health` → `{status, db, checkpointer, mode, backend_driver, version}`
- 阶段 5 预留：`POST /api/shopping-agent/run/stream`（SSE）、`POST /api/shopping-agent/resume`（HITL 断点恢复，用 checkpoint `thread_id`）。

### 5.2 业务后端服务（Mock，等价 Java 后端）

```
GET  /api/v1/goods/search?q=&category=&min_price=&max_price=&limit=
GET  /api/v1/goods/{goods_id}
POST /api/v1/stock/query      {"goods_ids": ["g_001","g_002"]}
POST /api/v1/coupon/query     {"goods_ids": ["g_001"], "user_id": "u_1001"}
POST /api/v1/admin/seed       {"reset": true}      # 灌演示数据
GET  /health
```

---

## 六、实施计划（里程碑）

> 人力假设：1 人全职。总计约 **10.5 人日**（不含 MaxKB 侧配置联调）。
> 每个里程碑都有「可验证出口」，任一里程碑结束都能独立演示。

| 里程碑 | 内容 | 产出文件 | 验收 | 工时 |
| --- | --- | --- | --- | --- |
| **M0 工程骨架** | uv 项目初始化、`src/` layout、Settings、structlog、errors、Makefile、`.env.example`、git 初始化 | `pyproject.toml`、`core/*`、`Makefile` | `uv sync && make lint && uvicorn ...:app` 起空服务，`/health` 200 | 0.5d |
| **M1 数据层 + 种子数据** | ORM 表、`engine.py` 方言分支、建表/alembic、seeds JSON + loader、services 业务规则（库存=on_hand-reserved、券门槛/有效期/适用范围/券库存/每人限用） | `db/*`、`domain/*`、`services/*`、`seeds/*` | `make seed` 后单测断言 12 商品 / 库存 / 8 券；`test_services.py` 绿 | 1.0d |
| **M2 业务后端服务** | FastAPI 业务 API、依赖注入、统一异常处理、请求日志 | `business_api/*` | curl 三个接口返回正确 JSON；8001 `/health` 200 | 1.0d |
| **M3 Adapter + Guardrail** | ABC + 三实现（inprocess/http/http_java 留位）、超时重试鉴权、前置入参校验、后置逐条校验、fencing、grounding | `backends/*`、`guardrails/*` | `test_backends.py`（三 driver 同断言表）+ `test_guardrails.py` 全绿；伪造字段被丢弃、非法 ID 被拒 | 1.5d |
| **M4 LangGraph 图** | State、routing 硬路由、supervisor（双模式）、3 Worker、summarize、builder、SQLite checkpointer | `graph/*` | `test_routing.py` 覆盖「库存>0 跳过券」「库存=0 走券」两条分支；`test_graph_e2e.py` 在无 LLM 下端到端出答案 | 2.0d |
| **M5 Agent API + 偏好记忆 + 鉴权** | run 接口、token 校验、context 注入、preference 读写与敏感过滤、`agent_request_log`、异常文案降级 | `agent_api/*`、`services/preference.py` | `test_api.py` 绿；同 session 二次提问能复用候选商品（checkpoint 生效）；错误 token → 401 | 1.5d |
| **M6 联调 + 文档 + 交付** | `scripts/demo_curl.sh`、README 快速开始、ARCHITECTURE / SHOPPING_RULES / API / GUARDRAILS / EXTENSION_DB_DOCKER / MAXKB_INTEGRATION | `docs/*`、`scripts/*`、`README.md` | 全新环境 `make bootstrap && make dev && make demo` 一条链跑通；文档齐全 | 1.5d |
| **M7 扩展位（可选，后置）** | `deploy/Dockerfile`+compose（本机不构建）、MySQL 切换脚本验证、SSE 流式 + HITL 断点、新 Worker（比价/订单/售后）、评估集（参考 commerce-agents 案例 schema） | `deploy/*`、`evals/*` | 后续里程碑 | — |

推荐执行顺序：**M0 → M1 → M2 → M3 → M4 → M5 → M6**，严格串行，每个里程碑结束先跑 `make lint && make test` 再进下一个。

frame.md 阶段映射：本仓库 M0–M6 覆盖 **阶段 2 全部 + 阶段 1 的「干净 LangGraph 工程 + 导购业务规范文档」交付物 + 阶段 3 的服务侧一半**；阶段 1 的 MaxKB 部署、阶段 3 的 MaxKB 工具配置属仓库外工作，以 `docs/MAXKB_INTEGRATION.md` 指导；阶段 4 的 Docker 打包推迟到 M7（本机无 Docker）。

---

## 七、依赖清单

```toml
# 运行时
fastapi, uvicorn[standard], pydantic>=2, pydantic-settings, python-dotenv
langgraph>=1.0, langgraph-checkpoint-sqlite, aiosqlite
langchain-core>=1.0
sqlmodel>=0.0.25          # 或 SQLAlchemy 2.0 ORM，二选一，默认 SQLModel（与 template-langgraph 一致）
httpx, tenacity, structlog, uuid6

# 开发/测试
pytest, pytest-asyncio, ruff, pyright, detect-secrets?  (可选)

# extras（本机不装，切换数据库时才装）
[project.optional-dependencies]
mysql    = ["pymysql>=1.1"]
postgres = ["psycopg[binary]>=3.3", "langgraph-checkpoint-postgres>=3.0"]
llm      = ["langchain-openai>=1.0"]
```

刻意**不引入**（template-langgraph 有但本机环境跑不动/用不上）：`psycopg*`、`supabase`、`mem0ai`、`langfuse`、`slowapi`+Redis、`prometheus-client`、`passlib/jose` 完整鉴权体系、`alembic` 保留但仅作为可选迁移工具、`duckduckgo-search`。这些在 `docs/ARCHITECTURE.md` 里标注为「可后补的平台能力」。

---

## 八、配置项（`.env`）

```
APP_ENV=dev
LOG_LEVEL=INFO
AGENT_MODE=rule_based                # rule_based | llm
AGENT_INTERNAL_TOKEN=dev-token       # MaxKB 透传 token 校验；置空则跳过校验并 WARN

DATABASE_URL=sqlite:///./data/business.db
CHECKPOINT_BACKEND=sqlite            # sqlite | postgres | memory（单测）
CHECKPOINT_DB_PATH=./data/checkpoints.db
BACKEND_DRIVER=inprocess             # inprocess | http | http_java
BUSINESS_API_BASE_URL=http://127.0.0.1:8001
BUSINESS_API_TOKEN=                  # 留空则跳过校验并 WARN（本机默认）
BUSINESS_API_TIMEOUT_S=5
BUSINESS_API_MAX_RETRY=2
AGENT_TOTAL_TIMEOUT_S=30             # 单请求总预算，超限走降级话术

OPENAI_API_KEY=                      # 留空 → 强制回落 rule_based
OPENAI_BASE_URL=
LLM_MODEL=gpt-4o-mini
LLM_TIMEOUT_S=20
MAX_GOODS_LIMIT=20
MAX_FENCED_CHARS=8000                # 外部文本进入 prompt 前的截断上限
SEED_ON_STARTUP=true                 # 仅 dev 环境生效
```

---

## 九、风险与对策

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| LangGraph 1.x API 与 frame.md 伪码（0.x 风格）有差异 | M4 返工 | M4 开始前先用最小 spike 脚本确认 `StateGraph(PydanticModel)` + `Command(goto=)` + `AsyncSqliteSaver` 在 1.x 的确切用法，再批量写节点 |
| Windows + SQLite 并发写锁（两服务同时开库） | 偶发 `database is locked` | 开 WAL + `busy_timeout=5000`；checkpointer 单独文件；业务写只在 seed/admin 路径，Agent 侧只读 |
| 表名/字段与未来 Java 后端不一致 | M3 `http_java` 落地时返工 | `GoodsOut/StockOut/CouponOut` 独立于 ORM 模型（DTO 层），Java 映射只需在 `http_java.py` 做一次 rename |
| 无 LLM 时验收不了「LLM 调度」 | 阶段 2 第 5 项不完整 | 用 `FakeLLM`（固定返回 `SupervisorDecision`）在单测覆盖 llm 分支，真实 llm 模式作为可选联调 |
| Guardrail 过严导致合法数据被丢弃 | 答案空、体验差 | 后置校验只**逐条丢弃**非法项并记 WARN + `meta.degraded`，整批仅在全部非法时才空；阈值与规则集中在 `GUARDRAILS.md` 可调 |
| 一次性交付过大难验证 | 集成期爆雷 | 严格按 M0–M6 串行，每个里程碑独立 `make test` 绿 + 可演示；M3/M4 之间设置硬性门禁 |

---

## 十、端到端验证清单（M6 出口）

```bash
cd /d/LLMProject/shopping/shopping-backend
make bootstrap          # uv sync + 建 data 目录 + 建表 + 灌种子
make dev                # 起 8001 业务后端 + 8000 Agent 服务（无 Docker）
make demo               # 跑 scripts/demo_curl.sh
```

必须通过的 7 条业务用例：

| # | query | 期望链路 | 期望结果要点 |
| --- | --- | --- | --- |
| 1 | `推荐 2000 元以内的手机` | supervisor→goods→stock(有货)→summarize | 3 款价格 ≤2000 的在售手机；`route_trace` 无 `coupon_worker` |
| 2 | `红米 Note13 有货吗` | supervisor→goods(解析ID)→stock→summarize | 先解析出真实 `goods_id`（`seen_goods_ids` 非空）再给可售数量；**不出现凭商品名拼的 ID** |
| 3 | `这款卖断了的耳机有优惠券吗` | supervisor→goods→stock(全 0)→coupon→summarize | 命中缺货 → 走券；给出可用券码与门槛 |
| 4 | `有什么满减活动` | supervisor→coupon→summarize | 仅券信息 |
| 5 | 后端接口人为超时（改 `BUSINESS_API_TIMEOUT_S=0`） | 任一路径 | 友好文案 + `meta.degraded=true`，HTTP 200，无堆栈 |
| 6 | 错误 token 调 run 接口 | — | 401，且不进入图 |
| 7 | 同 `session_id` 连问「推荐手机」→「第二款有货吗」 | checkpoint 复用 | 第二问基于 State 中第二款定位，不重新全量搜索（`seen_goods_ids` 持久化生效） |

补充自动化：`make test` 全绿（含 `test_routing.py` 两条硬路由分支、`test_guardrails.py` 防幻觉拒绝）、`make lint` 无 error。

---

## 十一、commerce-agents 范式移植对照表

| 借鉴点（来源） | 本仓库落地位置 |
| --- | --- |
| `StorefrontBackend` ABC + 每方法带 session context、身份不进工具入参 | `backends/base.py`、`domain/context.py` |
| `NotOffered` / `Unavailable` 双态领域异常 | `core/errors.py` |
| 执行器「永不抛异常」，失败转成模型可读文本 | `backends/*` + `guardrails/validate_output.py` |
| `ToolOutcome(result_text, events, is_error, blocked)` | Worker 节点统一返回结构 + `route_trace` |
| `seen_products` 溯源白名单 + 写操作 ID 必须在其中 | `state.seen_goods_ids` + `guardrails/grounding.py` |
| fence：外部文本清洗到不动点 | `guardrails/fencing.py` |
| 展示层 payload 只允许服务端字段（价格/标题/图）来自后端 | `summarize` 用模板渲染 state，模型只写 `reason` 文案 |
| `MemoryStore` 协议 + 敏感信息写入过滤（卡号/邮箱正则拦截） | `services/preference.py` |
| tier-1 偏好每轮自动注入（constraint 优先，上限 8 条） | `supervisor` / `summarize` prompt 组装 |
| 评估案例 JSON schema（calls_tool / never_calls / reply_omits / rubric） | M7 `evals/`（可选） |

---

## 十二、当前进度与下一步

**已完成（文档先行）**

| 交付物 | 位置 |
| --- | --- |
| 开发方案与实施计划（本文件） | `D:\LLMProject\shopping\DEVELOPMENT_PLAN.md` |
| 工程入口与状态 | `shopping-backend/README.md` |
| 导购业务规范（frame.md 阶段 1 交付物） | `shopping-backend/docs/SHOPPING_RULES.md` |
| 架构说明（含图定义、State 序列化策略、10 条 ADR） | `shopping-backend/docs/ARCHITECTURE.md` |
| 接口契约（两个服务 + driver 映射 + curl） | `shopping-backend/docs/API.md` |
| 护栏规则清单（7 层 + 15 条单测） | `shopping-backend/docs/GUARDRAILS.md` |
| 数据库 / Java 后端 / Docker 扩展指南 | `shopping-backend/docs/EXTENSION_DB_DOCKER.md` |
| MaxKB 集成指引（含 OpenAPI 片段与灰度回滚） | `shopping-backend/docs/MAXKB_INTEGRATION.md` |

**M0–M6 已实现并验证（本机：无 Docker、无 MySQL、无模型 Key）**

| 验证项 | 结果 |
| --- | --- |
| 单元测试 | `uv run pytest -q` → **222 passed**（11 个文件，全部无需网络/Key；含前端页面 2 条用例） |
| 端到端真实进程 | `uv run python scripts/run_demo.py` → **7/7**，含用例 5 指向不可达后端的降级实例 |
| 静态检查 | `uv run ruff check src tests scripts alembic` → All checks passed |
| LangGraph 前提 | `scripts/spike_langgraph.py` → 11/11 PASS，M4 前完成 |
| 迁移 | `tests/test_alembic.py`：`alembic upgrade head` 建表完整、幂等、可回滚，且与 ORM metadata 无结构漂移 |

实现期间相对本方案有 12 处修正（SQLModel 要求 timezone-aware UTC、`AutoString`/`expire_on_commit` 两处 ORM 行为、checkpointer 状态跨轮累加、`thread_id` 必填、httpx 注入 client 绕过 timeout/token、券 `display` 改为服务端派生、券归属改为分桶校验等），逐条记录在 `shopping-backend/docs/ARCHITECTURE.md` §10；其中 §4.2/§10 的路由与时间口径、§4.6 的 `http_java`、§8 的配置项、§7 的依赖清单（补 `alembic`、去 `uuid6`）都已回写到本文件对应位置。

另外两处工程落地层面的取舍：`seeds/*.json` 移入 `src/shopping_backend/seeds/`（随包发布，容器镜像与 `pip install` 都取得到）；本机 `make` 不可用，因此主入口改为 `scripts/`（`init_db.py` / `run_demo.py` / `run_dev.sh|.ps1` / `demo_curl.sh`），`Makefile` 作为 Linux/CI 的等价封装保留。

**剩余（M7，均未开工）**

1. `docker build` 实机验证与 MySQL 真实切换（`deploy/` 与 `docs/EXTENSION_DB_DOCKER.md` §3/§5 已就绪）；
2. MaxKB 侧外部工具配置（`docs/MAXKB_INTEGRATION.md` §2/§5，属仓库外人工操作）；
3. SSE 流式与 HITL 断点（`AgentRunOut` 与 checkpoint `thread_id` 已为此留好形状）；
4. 新 Worker（比价 / 售后 / 订单）与券叠加策略（`_STRATEGY_COUNTS` 扩展点）；
5. 用户领券记录表（当前据此拒绝在券接口接收 `user_id`，避免给出无法证实的可领结论）；
6. 模型驱动的偏好抽取（现为确定性抽取，只记用户明说的预算与品类）。
