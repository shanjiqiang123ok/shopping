# 数据库与部署扩展指南（EXTENSION_DB_DOCKER）

> 本机现状：**无 Docker、无 MySQL**，用 SQLite 跑通流程。
> 本文说明后续切换的**唯一改动点**、迁移步骤、验证脚本与常见坑。
> 设计目标：切数据库/上容器/接 Java 后端都**不改业务代码、不改图、不改护栏**。

---

## 一、扩展点总览

| 关注点 | 唯一改动点 | 需要改代码吗 |
| --- | --- | --- |
| 业务库 SQLite → MySQL/PostgreSQL | `.env` 的 `DATABASE_URL` + 安装对应 extra | ❌ |
| 连接池/PRAGMA 差异 | `db/engine.py`（方言分支已集中） | ❌（新增方言才改这里） |
| 表结构演进 | `alembic/versions/*` | ❌（迁移文件本身除外） |
| 检查点存储 | `CHECKPOINT_BACKEND=sqlite\|postgres\|memory` + `graph/checkpoint.py` | ❌ |
| 长期记忆/向量 | 无（已用 `user_preference` 表；pgvector 属新增能力） | 需要时改 `services/preference.py` |
| 业务数据来源 | `BACKEND_DRIVER=inprocess\|http\|http_java` + `BACKEND_URL` | ❌（仅 `http_java` 需补字段映射） |
| 模型 | `OPENAI_API_KEY` / `LLM_MODEL` / `AGENT_MODE` | ❌ |
| 容器化 | `deploy/`（已预置） | ❌ |

---

## 二、SQLite 阶段的既定约束（为切换铺路）

这些约束写在代码里，`tests/test_db_engine.py` 会断言，违反即红灯：

1. **主键用 `String(36)` UUID**，不用自增/AUTO_INCREMENT/序列 → 三种方言同一套 DDL。
2. **金额 `Integer` 分**，禁止 `Float`/`Numeric` 浮点歧义。
3. **不使用方言 `JSON` 类型**：`tags`、`scope_value` 用 `Text` + 应用层 `json.dumps/loads`。
4. **时间：Python 层一律 timezone-aware UTC**（`domain/context.utcnow()` + `ensure_utc()`），落库为 UTC 墙上时间。
   > 落地修正：原方案是 naive UTC，但 SQLModel 的默认列类型 `UTCDateTime` 会拒绝 naive 值（`Datetime values must have timezone information`）。带偏移实际上更安全：写入的永远是 UTC，不受 DB 服务器 session 时区影响。序列化后形如 `2026-12-31T15:59:59Z`。见 `docs/ARCHITECTURE.md` D3。
5. **枚举存 `String(16)` + 应用层校验**，不用 `sa.Enum(native_enum=True)`（PG 会建原生 enum，迁移麻烦）。
6. **不写方言专属 SQL**：不用 `ON DUPLICATE KEY UPDATE` / `INSERT … ON CONFLICT`；upsert 用「先 SELECT 再 INSERT/UPDATE」（在 `session_scope` 事务内）。
7. **索引显式命名**（`ix_goods_category` 等），MySQL 有 64 字符限制，SQLite 无 → 提前收敛。
8. **保留字规避**：表名 `goods/stock/coupon/user_preference/agent_request_log`，列名避开 `order`/`group`/`key`（`pref_key` 已改名）。
9. 查询只用 SQLAlchemy Expression / SQLModel select，字符串比较不依赖 `COLLATE`。

SQLite 专属优化（只在 `engine.py` 的 sqlite 分支）：`PRAGMA journal_mode=WAL`、`busy_timeout=5000`、`foreign_keys=ON`、`check_same_thread=False`。

---

## 三、切换到 MySQL（后续操作，现在不执行）

### 3.1 前置

```bash
# 1) 安装 MySQL 驱动（extra，不在默认依赖里，避免本机装不上）
uv sync --extra mysql                # pymysql

# 2) 建库建用户（字符集必须是 utf8mb4）
mysql -e "CREATE DATABASE shopping CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
mysql -e "CREATE USER 'shopping'@'%' IDENTIFIED BY '...'; GRANT ALL ON shopping.* TO 'shopping'@'%';"
```

### 3.2 改配置

```
DATABASE_URL=mysql+pymysql://shopping:***@127.0.0.1:3306/shopping?charset=utf8mb4
SEED_ON_STARTUP=false        # 生产禁止启动即写数据
```

### 3.3 迁移（不要依赖 create_all）

```bash
# 首次：跑基线。之后的列变更用 autogenerate（差异自动对比 ORM metadata）
uv run alembic upgrade head
uv run alembic current       # 期望 0001_baseline (head)
```

`alembic/env.py` 从 `Settings.resolved_database_url` 取 URL（**不硬编码 postgresql://**，这是相对 template-langgraph 的一处必要修正），并按方言设置 `render_as_batch`（SQLite 需要，MySQL/PG 忽略）。

基线 `0001_baseline` 刻意由 `SQLModel.metadata.create_all(tables=...)` 生成，而不是手写 DDL：模型就是这个项目的 schema 唯一事实来源，手抄一份 0001 会在第一次列改名时悄悄漂移。`tests/test_alembic.py` 用 `alembic.autogenerate.compare_metadata` 断言「跑完迁移的库」与「metadata」之间没有 add/remove table、add/remove column 差异，把这条保证钉成测试。checkpoint 表不在 metadata 里，由 `langgraph-checkpoint` 自己的 `setup()` 管，迁移不会碰它。

### 3.4 灌数据

```bash
uv run python scripts/init_db.py --backend mysql   # 含方言自检：打印 dialect / server version
uv run python -m shopping_backend.seeds.loader      # 或 POST /api/v1/admin/seed（仅 dev）
uv run python scripts/switch_db_example.py verify   # 逐表行数 + 关键业务断言（券过滤、库存汇总）
```

### 3.5 检查点是否一起切

| 选择 | 配置 | 说明 |
| --- | --- | --- |
| 保持 SQLite（推荐先这样） | `CHECKPOINT_BACKEND=sqlite` | 业务库换 MySQL 与检查点无关，风险最小；单副本足够 |
| 换 Postgres | `CHECKPOINT_BACKEND=postgres` + `POSTGRES_CHECKPOINT_URL=postgresql://...` + `uv sync --extra postgres` | 多副本必需（本地 volume 不能共享）。未装 extra 或未给 URL 时启动直接抛 `ConfigError`，消息里写明要跑哪条命令，不做静默回退 |
| 单测/临时 | `CHECKPOINT_BACKEND=memory` | `InMemorySaver`，不持久化；`tests/` 全用它 |

三种取值都在 `graph/checkpoint.py::open_checkpoint_provider` 里，未知取值同样抛 `ConfigError`（有用例 `test_memory_backend_and_unknown_kind` 钉住）。

### 3.6 MySQL 特有问题清单

| 坑 | 现象 | 处置 |
| --- | --- | --- |
| 字符集非 utf8mb4 | 中文商品标题变 `???` | 建库强制 utf8mb4；连接串带 `charset=utf8mb4` |
| 长字符串索引 | `Spec 1071: Index column size too large` | 索引列 ≤191 字符（utf8mb4 下 767 字节）；`title` 不建索引，用 `category`+`sku` |
| `pool_recycle` 未设 | 8 小时空闲后 `MySQL server has gone away` | `engine.py` mysql 分支固定 `pool_recycle=3600, pool_pre_ping=True` |
| naive UTC 与时区 | 展示时间差 8 小时 | 展示层统一 `Asia/Shanghai` 转换；DB 只存 UTC |
| 隔离级别 | 高并发下读到自己未提交写 | 默认 `REPEATABLE READ` 即可；只读接口显式 `START TRANSACTION READ ONLY` 属可选优化 |

---

## 四、切换到真实 Java 后端

`frame.md` 的终态是 Agent → HTTP → Java。`backends/http_java.py` **已实现**为几张映射表（不是占位、不抛 `NotImplementedError`），因为对端协议不同只是"改名 + 换单位"，把这层收敛成数据比写成代码更容易核对：

| 表 | 作用 |
| --- | --- |
| `PATHS` | 我们的操作名 → 对方 URL |
| `ROWS_PATH` | 响应体里取数的位置，支持点分路径（`data.resultList`） |
| `REQUEST_ALIASES` | 我们的参数名 → 对方参数名（`limit → pageSize`、`goods_ids → skuIds`） |
| `ROW_ALIASES` | 对方字段名 → 我们 DTO 字段名（`productId → id`、`couponCode → code`） |
| `CONVENTIONS` | 随字段一起做的单位换算：`priceYuan`/`startAmount`/`discountAmount` 元→分，`discountRate` 0.9→千分比 900 |
| `SHELF_STATUS_MAP` / `SCOPE_MAP` / `DISCOUNT_TYPE_MAP` | 枚举词表：`ON_SALE→on_sale`、`CATEGORY→category`、`FULL_REDUCE→amount` |

落地步骤（你方提供 Java 接口后）：

1. 把上面几张表的值改成真实约定——这是唯一需要写的映射代码；
2. 配 `BACKEND_DRIVER=http_java`、`BUSINESS_API_BASE_URL=http://java-host:8080`、`BUSINESS_API_TOKEN=<Java 侧内部凭证>`；
3. Java 侧必须满足三条契约（写进联调 checklist；违反时护栏会兜住，但会污染 `degraded` 指标并掩盖对端 bug）：
   - 库存查询对**未知 ID 也要返回记录且 `available=0`**（否则由 Agent 按 R-S2 补零）；
   - 商品列表**不得返回下架商品**（Agent 会以 `not_on_sale` 丢弃并记 WARN）；
   - 金额单位与 `docs/API.md` 一致（分），或明确落在 `CONVENTIONS` 里做元↔分换算；
4. `uv run pytest tests/test_backends.py -k java` —— 用例里的 `FakeJavaServer` 用 `httpx.MockTransport` 产出**Java 风格响应**（`skuIds`、`priceYuan`、`ON_SALE`、`discountRate`、`data.couponMap`），与 `inprocess`/`http` 跑同一张断言表，**不需要 Java 服务在线**即可验证映射；
5. 灰度顺序：`inprocess` → `http`（本机 Mock，验证真实 HTTP 拓扑）→ `http_java`（真实）。三级共用同一张图与同一套护栏；`scripts/run_demo.py` 目前已同时跑 `inprocess` 与 `http`（后者故意指向不可达端口以验证降级），接 Java 时把第三个进程换成真实对端即可。

派生字段 `display`（券的"满¥X减¥Y"文案）**不接受 wire 值**：适配器在校验后统一重算（D9），因此对端漏传或传错都不会让话术失去门槛（R-C2）。

---

## 五、Docker 化（`deploy/` 已预置，本机不构建）

### 5.1 资产

```
deploy/Dockerfile            # 多阶段：uv 装依赖 → python:3.13-slim 运行；非 root；HEALTHCHECK
deploy/docker-compose.yml    # shopping-agent(8000) + shopping-business(8001) + mysql8 + healthcheck
deploy/env.production.example
deploy/mysql.cnf.example     # utf8mb4 / max_connections 等基线
```

`docker-compose.yml` 关键片段（已写入，等你有 Docker 时直接 `make docker-up`）：

```yaml
services:
  mysql:
    image: mysql:8.4
    environment: { MYSQL_DATABASE: shopping, MYSQL_USER: shopping, MYSQL_PASSWORD: ${DB_PASSWORD}, MYSQL_ROOT_PASSWORD: ${DB_ROOT_PASSWORD} }
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "127.0.0.1", "-uroot", "-p${DB_ROOT_PASSWORD}"]
      interval: 5s
      retries: 20
  business:
    build: { context: .., dockerfile: deploy/Dockerfile, target: business }
    environment: { DATABASE_URL: mysql+pymysql://shopping:${DB_PASSWORD}@mysql:3306/shopping?charset=utf8mb4 }
    depends_on: { mysql: { condition: service_healthy } }
    healthcheck: { test: ["CMD", "python", "-c", "import httpx;httpx.get('http://127.0.0.1:8001/health',timeout=2)"] }
  agent:
    build: { context: .., dockerfile: deploy/Dockerfile, target: agent }
    environment: { BACKEND_DRIVER: http, BUSINESS_API_BASE_URL: http://business:8001, CHECKPOINT_DB_PATH: /data/checkpoints.db }
    depends_on: { business: { condition: service_healthy } }
    volumes: [ "checkpoints:/data" ]
```

要点：
- **同一镜像两个 target**（`agent` / `business`），差异只有启动命令，避免双份构建。
- Agent 无状态 → 可多副本；`checkpoints` 用 named volume（多副本时换 Postgres 检查点，见 §3.5）。
- SQLite 文件不进镜像层，挂 volume；`data/` 在 `.dockerignore` 里。
- 健康检查全部走 `/health`（含 DB 与 checkpointer 状态），MaxKB 侧超时 5s 起。

### 5.2 上线顺序（有 Docker 后）

> 本机没有 `make`（Windows），所以下面两步在有 make 的机器上跑；等价的原始命令写在 `deploy/Dockerfile` 头部注释里，任何环境都能直接用。
> 本机的等价入口是 `uv run python scripts/init_db.py`（建库灌数）+ `uv run python scripts/run_demo.py`（起进程 + 7 条用例）。

```bash
make docker-build                       # 构建两 target
DB_ROOT_PASSWORD=... DB_PASSWORD=... AGENT_TOKEN=... BUSINESS_TOKEN=... \
  docker compose -f deploy/docker-compose.yml up -d mysql
docker compose -f deploy/docker-compose.yml run --rm business alembic upgrade head
docker compose -f deploy/docker-compose.yml up -d business agent
uv run python scripts/run_demo.py       # 改打 compose 暴露的端口，跑 7 条端到端用例
docker compose logs -f agent | grep route_trace
```

---

## 六、本机（无 Docker/无 MySQL）与目标环境的差异对照

| 维度 | 本机现状 | 目标环境 | 切换成本 |
| --- | --- | --- | --- |
| 业务库 | `data/business.db`（单文件） | MySQL 8.4 | 1 个 env + 1 个 extra + `alembic upgrade head` |
| 建表 | `create_all`（dev） | alembic 迁移 | 迁移文件已方言中立 |
| 检查点 | SQLite 文件 | Postgres（多副本时） | 1 个 env + extra |
| 服务拓扑 | `inprocess`，单进程 | `agent` + `business`/Java 两进程/容器 | 1 个 env |
| LLM | `rule_based`（无 Key） | `llm`（网关 Key） | 2 个 env |
| 观测 | 结构化日志 + `route_trace` + 自绘请求日志表 | + LangSmith / Prometheus | 文档化后补，代码留 config 透传 |
| 鉴权 | 内部静态 token | MaxKB JWT + 内部 token | 无需改（边界不变） |

**结论**：本阶段所有「本机妥协」都收敛在 5 个环境变量与 2 个 extras 上；业务流程一旦跑通，切换动作是配置与迁移，不是重构。
