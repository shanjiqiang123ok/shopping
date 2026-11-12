# shopping-backend

> MaxKB + LangGraph 导购 Agent 服务 + 内置 Mock 业务后端
> 方案总纲见仓库根目录 `../DEVELOPMENT_PLAN.md`，本文件是工程入口。

## 一、这个项目是什么

按 `frame.md` 的组合实施方案落地：**独立部署的 LangGraph 导购 Agent 服务**，MaxKB 通过「外部自定义 HTTP 工具」调用它，Agent 再通过适配器层读取商品/库存/优惠券业务数据。

本机约束是**没有 Docker、没有 MySQL、没有模型 API Key**，所以工程做成「一条命令跑通完整业务流程」的形态，同时把切换点全部收敛到环境变量：

| 组成 | 端口 | 说明 |
| --- | --- | --- |
| Agent 服务 `agent_api` | 8000 | LangGraph 图编排、Supervisor + 3 Worker、双层护栏、SQLite checkpointer |
| 业务后端服务 `business_api` | 8001 | 商品/库存/优惠券接口，充当 `frame.md` 里「你的 Java 后端」的位置 |
| 持久化 | — | `data/business.db`（业务数据）+ `data/checkpoints.db`（流程快照），纯 SQLite |

业务数据从哪来，由一个变量决定，图代码零改动：

```
BACKEND_DRIVER=inprocess     # 默认：Agent 直连同仓库 Service 层，不占端口、无网络
BACKEND_DRIVER=http          # Agent 通过 HTTP 调用本机 8001（演示真实拓扑）
BACKEND_DRIVER=http_java     # 切到真实 Java 后端，只改两张映射表
```

## 二、状态：M0–M6 已完成并验证

| 里程碑 | 内容 | 状态 |
| --- | --- | --- |
| — | 开发方案与实施计划 | ✅ `../DEVELOPMENT_PLAN.md` |
| — | 工程文档集 | ✅ `docs/` 6 篇 |
| M0 | 工程骨架（pyproject/uv、Settings、structlog、errors、Makefile） | ✅ |
| M1 | 数据层 + 种子数据 + 业务规则（库存汇总、券六步判定、偏好写过滤） | ✅ 44 测试 |
| M2 | 业务后端服务（5 个接口 + health + admin seed） | ✅ 14 测试 |
| M3 | BackendAdapter 三驱动 + 双层护栏（7 层） | ✅ 23 + 38 测试 |
| M4 | LangGraph 图（State / 硬路由 / 双模式 Supervisor / 3 Worker / summarize / checkpoint） | ✅ 17 + 41 + 15 + 7 测试 |
| M5 | Agent API + token 鉴权 + 偏好回写 + 请求日志 | ✅ 17 测试 |
| M6 | 脚本、迁移、容器资产、端到端验证 | ✅ 4 测试；合计 **222 全绿 + 7/7 真实进程用例** |
| — | 前端联调页面 `/chat`（v1，零构建单文件） | ✅ 2 测试；方案见 [`实现方案.md`](实现方案.md) |
| M7 | Docker 构建、MySQL 实机切换、HITL 断点、评估集 | ⏳ 可选后置（`deploy/`、`alembic/` 已就绪，本机不构建） |

复现验证：

```bash
uv run pytest -q              # 222 passed，无需外网/Key/Docker
uv run python scripts/run_demo.py   # 起 3 个真实进程，7/7 用例通过
uv run ruff check src tests scripts alembic   # All checks passed
```

## 三、快速开始

> 本机没有 `make`（Windows），所以**主入口是 scripts**；`Makefile` 是给 Linux/CI 的等价封装。

```bash
cd D:/LLMProject/shopping/shopping-backend

# 1) 装依赖 + 建表 + 灌演示数据
uv sync --group dev
uv run python scripts/init_db.py

# 2) 跑通全流程（自带 7 条用例校验，会自己起进程、自己收摊）
uv run python scripts/run_demo.py

# 3) 手工起服务调试
uv run python -m uvicorn shopping_backend.agent_api.main:app --port 8000      # Agent
uv run python -m uvicorn shopping_backend.business_api.main:app --port 8001   # 业务后端
#   或： bash scripts/run_dev.sh  /  powershell scripts/run_dev.ps1
#   手工 curl： bash scripts/demo_curl.sh

# 4) 交互式接口文档
#   http://127.0.0.1:8000/docs   http://127.0.0.1:8001/docs
```

单条请求（默认 `inprocess`，只需 8000）：

```bash
curl -s -X POST http://127.0.0.1:8000/api/shopping-agent/run \
  -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{"user_id":"u_1001","session_id":"s_demo","query":"推荐 2000 元以内的手机，有券吗"}'
```

实际回答（数字全部来自业务库，无一句模型编造）：

```
为你找到 3 款 2000 元以内的手机在售商品：
1) realme GT Neo 6 5G 手机 ¥1599.00｜仅2件（紧张）｜realme
2) Redmi Note 13 Pro 5G 手机 ¥1699.00｜现货12件｜Redmi
3) Redmi K70 5G 手机 ¥2000.00｜现货20件｜Redmi
可用券：满¥1500.00减¥100.00（券码 M1500_100，2026-12-31 前有效）｜适用：realme GT Neo 6 5G 手机、Redmi Note 13 Pro 5G 手机
可用券：满¥2000.00减¥150.00（券码 M2000_150，2026-12-31 前有效）｜适用：Redmi K70 5G 手机
想只看有货的、或者换个预算再筛一次，告诉我就行。
```

**无 LLM 也能跑**：`AGENT_MODE=rule_based`（默认）不依赖模型与外网；图结构、路由分支、护栏与 `llm` 模式完全相同，配好 `OPENAI_API_KEY` 后改 `AGENT_MODE=llm` 即启用模型做意图判断与推荐理由文案。

## 四、目录结构（实际落地）

```
shopping-backend/
├── pyproject.toml  uv.lock  Makefile  README.md  .env.example  .python-version
├── alembic/                # 方言中立迁移（0001_baseline = 由 ORM metadata 生成）
├── src/shopping_backend/
│   ├── core/               # config / logging / errors / ids / http(共享中间件与异常形状)
│   ├── db/                 # ★ engine.py 是唯一知道 SQL 方言的地方
│   ├── domain/             # models_orm(表) + schemas(DTO 契约) + enums + context + formatting
│   ├── backends/           # ★ StorefrontBackend ABC + inprocess / http / http_java
│   ├── guardrails/         # ★ validate_input / validate_output / fencing / grounding
│   ├── graph/              # ★ state / routing / supervisor / builder / checkpoint / deps
│   │   └── nodes/          # goods / stock / coupon Worker + summarize + presentation(话术模板)
│   ├── llm/                # registry + service（超时/重试/降级/结构化输出）+ NullLLM
│   ├── services/           # ★ 业务规则层（goods / stock / coupon / preference）
│   ├── agent_api/          # FastAPI :8000（runtime.py 管生命周期）
│   ├── business_api/       # FastAPI :8001
│   └── seeds/              # 演示数据 JSON（随包发布）+ loader（用 DTO 校验后再落库）
├── scripts/                # init_db / run_demo / reset_db / spike_langgraph / run_dev.* / demo_curl.sh
├── tests/                  # 222 用例，含 fakes.py（FakeLLM / RaisingBackend）
├── deploy/                 # Dockerfile(双 target) + compose + env/mysql 样例（本机不构建）
└── docs/                   # 架构 / 业务规范 / API / 护栏 / 扩展 / MaxKB 集成
```

## 五、文档索引

| 文档 | 用途 | 对应 frame.md |
| --- | --- | --- |
| [`运行.md`](运行.md) | **启动方案**：三种起法、前端说明（本项目无自带前端）、配置矩阵、故障速查 | 阶段 1/4 |
| [`docs/SHOPPING_RULES.md`](docs/SHOPPING_RULES.md) | **导购业务规范**：数据字典、意图与槽位、F1–F8 流程硬约束、O1–O9 输出规范、偏好与降级 | 阶段 1 交付物 |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 分层与职责、图定义、State 序列化、时序、ADR，以及**§10 实现期发现** | 阶段 2 |
| [`docs/API.md`](docs/API.md) | 两个服务的真实契约（示例取自实际响应）+ curl | 阶段 2 第 7 项 |
| [`docs/GUARDRAILS.md`](docs/GUARDRAILS.md) | L1–L7 护栏清单与实现位置、失败动作 | 阶段 2 第 3 项 |
| [`docs/EXTENSION_DB_DOCKER.md`](docs/EXTENSION_DB_DOCKER.md) | SQLite → MySQL/PG、接 Java、上 Docker | 阶段 4 |
| [`docs/MAXKB_INTEGRATION.md`](docs/MAXKB_INTEGRATION.md) | MaxKB 外部 HTTP 工具配置、token 透传、两套会话、灰度回滚 | 阶段 3 |

## 六、三条红线（已落实）

1. ❌ **不修改 MaxKB 源码** —— 只提供 `docs/MAXKB_INTEGRATION.md` 的外部工具配置，`agent_api` 与 MaxKB 之间只有一层 HTTP。
2. ❌ **不直接复用 commerce-agents 源码** —— 移植的是范式：Backend ABC + session 身份不进工具入参、`BackendResult` 结果形状、`seen_goods_ids` 溯源白名单、fence 文本清洗、偏好 tier-1 注入与写过滤。对照表见 `../DEVELOPMENT_PLAN.md` §11。
3. ✅ **业务顺序由代码兜底、护栏优先代码校验** —— 商品→库存→优惠券由 `graph/routing.py` 的 ConditionalEdge 决定，`AGENT_MODE=llm` 也改不了拓扑；回答里的商品 ID / SKU / 券码 / 价格 / 库存数必须能在 State 中溯源，否则整行剔除（`guardrails/grounding.py`）。
