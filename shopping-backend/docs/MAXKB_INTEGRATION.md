# MaxKB 集成指引（阶段 3）

> 原则（`frame.md` 关键约束第 1 条）：**不修改 MaxKB 源码**，只用「外部自定义 HTTP 工具」接入，保证 MaxKB 可升级。
> 本文是**人工操作指引**：MaxKB 侧配置由你在 MaxKB 界面完成，本仓库只提供契约、样例与异常约定。
> 本机不部署 MaxKB —— 集成前先用 `scripts/demo_curl.sh`（等价于 MaxKB 的调用）验证服务侧已就绪。

---

## 一、集成拓扑与边界

```
MaxKB 应用（对话）                     shopping-backend
├─ 用户体系 / 登录鉴权                 ├─ 内部 token 校验（服务间）
├─ 会话与对话历史                      ├─ checkpoint 流程快照（thread_id）
├─ RAG 知识库 / 模型网关               ├─ LangGraph 编排 + 硬路由
├─ 简单助手（不调工具的问题）           ├─ 双层护栏 + 防幻觉
└─ 外部 HTTP 工具 ──── POST ────────▶ └─ /api/shopping-agent/run
```

| 能力 | 归属 | 备注 |
| --- | --- | --- |
| 用户是谁、有没有权限 | MaxKB | Agent 只校验调用来源 token，不做用户体系 |
| 聊过什么 | MaxKB | Agent 不存对话消息 |
| 导购流程走到哪一步、候选商品是什么 | shopping-backend（checkpointer） | 靠 `session_id` 对齐 |
| 业务事实（商品/库存/券） | 业务后端（本工程 Mock 或 Java） | Agent 只是取数与编排 |
| 商品知识问答（参数含义、选购科普） | MaxKB RAG | **不要**塞进 Agent，Agent 只做结构化查询 |

---

## 二、工具定义（MaxKB「工具」→「外部 API 工具」）

### 2.1 OpenAPI 片段（可直接用于 MaxKB 的 OpenAPI 导入）

```yaml
openapi: 3.0.1
info: { title: shopping-agent, version: 0.1.0 }
servers: [ { url: "http://<agent-host>:8000" } ]
paths:
  /api/shopping-agent/run:
    post:
      operationId: shoppingAgentRun
      summary: 电商导购：查商品、查库存、查优惠券并给出推荐
      description: >-
        当用户询问「有什么推荐 / 某商品有货吗 / 有没有优惠券或满减活动」时调用。
        入参直接透传用户原话，不要自行改写价格或商品名。
        返回的 answer 字段可直接回复用户，data 字段为结构化事实。
      parameters:
        - in: header
          name: Authorization
          required: true
          schema: { type: string }
          description: "Bearer <内部调用凭证>"
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [user_id, session_id, query]
              properties:
                user_id:    { type: string, description: "当前登录用户 ID" }
                session_id: { type: string, description: "当前会话 ID" }
                query:      { type: string, description: "用户原始问题" }
                tenant_id:  { type: string, default: default }
      responses:
        "200":
          description: OK
          content:
            application/json:
              schema:
                type: object
                properties:
                  answer: { type: string }
                  data:   { type: object }
                  meta:   { type: object }
```

### 2.2 变量映射（MaxKB 工具入参 ↔ 系统变量）

| 工具字段 | MaxKB 变量来源 | 说明 |
| --- | --- | --- |
| `user_id` | 系统变量「当前用户 ID」 | MaxKB 登录后自带，**不要让模型填** |
| `session_id` | 系统变量「会话 ID」 | 决定 checkpoint 连续性；每轮必须稳定 |
| `query` | 用户输入原文 | 原样透传，不做前置改写（改写会破坏槽位正则） |
| `tenant_id` | 固定 `default`（或工作空间 ID） | 多租户阶段再用 |
| `preference_memory` | 可选：会话变量里的偏好摘要 | 见 §4 |
| Header `Authorization` | 应用「密钥池 / 环境变量」 | **禁止**写进提示词或对话上下文 |

> MaxKB 高级设置里把该工具的超时设为 **30s**（与 `AGENT_TOTAL_TIMEOUT_S` 对齐），失败重试 **0 次**（Agent 内部已按只读语义重试，外层重试会放大流量并重复消耗模型额度）。

---

## 三、异常适配约定（阶段 3 第 2 项）

Agent 侧已保证「降级也返回 200 + 友好文案、绝不抛堆栈」，MaxKB 侧只需按状态码分流：

| Agent 返回 | MaxKB 工具行为 | 建议话术 |
| --- | --- | --- |
| 200 + `meta.degraded=false` | 用 `answer` 回复 | 原文即可，不要再让模型复述数字 |
| 200 + `meta.degraded=true` | 用 `answer` 回复 | 已在服务端给出「暂时无法获取商品信息…」 |
| 401 | 工具失败（配置问题） | 运维告警，不暴露给用户 |
| 422 | 工具失败（参数问题） | 提示「换个说法再问我」；同时排查 `query` 是否被前置改写 |
| 5xx / 连接失败 / 超时 | 工具失败 | MaxKB 默认异常文案 + 日志 |

排查线索优先看 `meta`：`route_trace`（走了哪条分支）、`error_msg`（哪个域失败）、`counts`（拿到几条数据）。

---

## 四、记忆打通（阶段 3 第 3 项）

两套记忆并存，各自职责不重叠：

| 记忆 | 存储 | 生命周期 | 用途 |
| --- | --- | --- | --- |
| 对话历史 | MaxKB | 会话 | 上下文连贯、UI 展示 |
| 流程状态 | checkpointer（`thread_id = tenant:session`） | 会话（可清理） | 第二轮「第二款有货吗」能定位候选 |
| 用户偏好 | `user_preference` 表（本工程） | 跨会话 | 预算、品类倾向、禁忌 |

可选打通方案（按侵入度递增）：

1. **单向（推荐起步）**：Agent 每轮结束异步抽取偏好写入自己的表；MaxKB 不动。
2. **摘要回传**：Agent 在响应 `meta.preference_summary`（如 `预算≤2000；偏好品类=手机`）里给出摘要，MaxKB 存进会话变量，下轮作为 `preference_memory` 入参带回。合并规则：**服务端表优先**，MaxKB 传入只补缺。
3. **RAG 共享**：把偏好写入 MaxKB 知识库/上下文段落，让非导购问题也能引用。属阶段 5，需要额外开发且要注意 §六 的敏感信息规则。

---

## 五、集成自检步骤

```bash
# A. 服务侧（本机，无 MaxKB 也能完成）
make bootstrap && make dev
make demo                         # 7 条用例全绿 → 服务侧就绪
curl -s http://127.0.0.1:8000/health

# B. 网络可达性（从 MaxKB 所在主机执行）
curl -s -X POST http://<agent-host>:8000/api/shopping-agent/run \
  -H "Authorization: Bearer <AGENT_INTERNAL_TOKEN>" -H "Content-Type: application/json" \
  -d '{"user_id":"probe","session_id":"probe1","query":"推荐 2000 元以内的手机"}'

# C. MaxKB 侧
#   1) 新建/打开导购应用 → 工具 → 添加「外部 API 工具」→ 用 §2.1 OpenAPI 导入或手工填
#   2) 配置 Authorization（密钥池）与 §2.2 变量映射
#   3) 应用提示词里显式声明工具使用边界（见 §5.1）
#   4) 关掉该工具的「失败自动重试」，超时 30s
#   5) 对话验证：分别提问选品 / 库存 / 券三类问题，检查是否真的调用了工具

# D. 端到端核验清单
#   - 同会话连续两问能定位上一轮候选（checkpoint 生效）
#   - Agent 回答里的价格与业务后端返回值一致（护栏生效）
#   - 人为把 BUSINESS_API_TIMEOUT_S 调到 0，MaxKB 显示友好文案而非堆栈（降级生效）
```

### 5.1 MaxKB 应用提示词建议片段

```
你在导购场景中只做两件事：调用 shoppingAgentRun 工具、把工具返回的 answer 原样呈现。
- 用户问商品/库存/优惠相关问题时必须调用工具，禁止凭记忆回答价格、库存、券码。
- 不要改写工具返回中的任何数字（价格、库存、优惠金额、券码）。
- 与导购无关的问题走你自己的能力（含 RAG），不要调用工具。
- 用户要求下单/付款/改价时，说明本助手只负责查询与推荐。
```

---

## 六、灰度与回滚（阶段 4）

| 阶段 | 流量 | 前置门禁 | 回滚方式 |
| --- | --- | --- | --- |
| 内部自测 | 白名单账号 | `make test` + `make demo` 全绿 | MaxKB 移除工具即可（不影响其他助手） |
| 1%–10% | 按用户尾号 | `agent_request_log` 连续 1 天无 `degraded` 突增 | 同上，秒级 |
| 50% | — | 意图分布与 `route_trace` 分支占比符合预期 | — |
| 100% | 全量 | 护栏丢弃率 < 1%，无编造价格工单 | 保留 Agent 服务但 MaxKB 侧停用工具 |

监控抓手（本阶段用 `agent_request_log` + 日志，不引入 Prometheus）：

- `degraded` 比例、`latency_ms` P95、护栏 `rows_dropped` 计数、`intent` 分布、grounding 剔除行数（>0 需告警，说明模型试图改写事实）。

---

## 七、不做清单（避免集成期返工）

- ❌ 不把 LangGraph 的 State 塞进 MaxKB 会话变量（两套体系混用会导致恢复语义不清）
- ❌ 不让 MaxKB 模型直接传 `goods_id` 给 Agent（Agent 侧 `extra="forbid"` 会拒，见 `docs/GUARDRAILS.md` §二）
- ❌ 不在 MaxKB 里做商品数据的二次加工展示（数字以 Agent `data` 为准）
- ❌ 不依赖 MaxKB 的重试来兜 Agent 的超时（Agent 内部已降级）
- ❌ 不为集成而 fork MaxKB
