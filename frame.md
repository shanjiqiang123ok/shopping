# MaxKB + template-langgraph + commerce‑agents 组合实施方案
> 核心原则：不是代码合并，是能力松耦合组合；HTTP跨服务调用，不侵入MaxKB源码；commerce‑agents只作为设计范式参考，不直接部署
 - MaxKB项目目录：D:\LLMProject\study-project\MaxKB
 - template-langgraph项目目录：D:\LLMProject\study-project\fastapi-langgraph-agent-production-ready-template
 - commerce‑agents项目目录：D:\LLMProject\study-project\commerce-agents

## 整体架构
户 / 运营
↓
MaxKB（Web 聊天界面、鉴权、会话、RAG、模型网关、日志）
↓【自定义 HTTP 工具调用】（MaxKB 外部工具）
独立 LangGraph 导购服务（基于 template-langgraph 改造 + 吸收 commerce‑agents 护栏）
├─ 适配器层（BackendAdapter，来自 commerce‑agents 思想）
├─ Skill + Guardrail 校验（入参校验、返回值 Schema 校验、防幻觉）
├─ State、Supervisor、3 个 Worker 节点（商品 / 库存 / 优惠券）
├─ 条件路由（代码硬路由 + LLM 调度）
├─ LangSmith 埋点、Checkpoint 持久化
↓ HTTP
你的 Java 后端（商品、库存、优惠券接口）

## 一、三者分工清单
|组件|承担能力|不承担能力|
| ---- | ---- | ---- |
|MaxKB|聊天前端、会话存储、用户/租户权限、向量知识库RAG、模型统一接入、限流、简单助手、外部HTTP工具调用、对话历史展示|复杂多Agent编排、代码级条件路由、结构化State、人工断点、业务层防幻觉护栏、导购业务规则|
|template-langgraph|LangGraph图编排、Supervisor主管调度、Worker子节点、TypedDict/Pydantic结构化State、条件边、checkpoint、LangSmith链路追踪、并行/子图|业务校验、适配器、导购规则、前端、权限、RAG、会话持久化|
|commerce‑agents|**提供设计范式**：Backend适配器抽象、Skill契约、前置+后置护栏、数据来源校验、用户偏好记忆、导购输出规范|编排引擎、前端、会话、权限、HTTP服务、断点；不直接部署|

## 二、分阶段实施方案（共5阶段）
### 阶段1：基础部署（0开发，底座跑通）
1. Docker部署MaxKB，配置模型接入；新建导购应用
2. 在MaxKB创建【外部自定义HTTP工具】
    - 工具入参：user_id、query、session_id
    - 首期同步调用，不做人工断点
3. 准备template-langgraph工程：保留Supervisor、State、FastAPI骨架，删除无关示例
4. 研读commerce‑agents，提取规范文档：Backend适配器、Skill Schema、护栏规则、偏好记忆范式

**交付物：MaxKB可用、干净LangGraph工程、导购业务规范文档**

### 阶段2：开发LangGraph导购服务（核心开发）
> 在template-langgraph骨架上落地，融合commerce‑agents护栏思想
1. 定义State（Pydantic）
```python
class ShoppingAgentState(BaseModel):
    user_id: str
    session_id: str
    query: str
    preference_memory: dict
    goods_list: list
    stock_map: dict
    coupon_map: dict
    error_msg: str | None
    final_answer: str | None
2. 实现 BackendAdapter 适配器层
   - 抽象 StorefrontBackend，方法：search_goods /get_stock/get_coupon
   - HttpJavaBackend 实现，统一 HTTP、超时、重试、鉴权、异常捕获
3. 实现 Skill + 双层 Guardrail 护栏
   - 前置护栏：入参 Schema 校验
   - 后置护栏：Java 返回结果校验，禁止 LLM 编造字段
4. 开发 3 个 Worker Node：商品 Worker、库存 Worker、优惠券 Worker
5. Supervisor + 混合路由
   - 代码硬路由（ConditionalEdge）：商品→库存；库存 > 0 直接汇总；库存 = 0 走优惠券
   - LLM 路由：处理开放导购意图判断
6. 汇总节点：仅读取校验后的 state 数据生成回答
7. 封装 FastAPI 接口 POST /api/shopping-agent/run，供 MaxKB 调用
8. 接入 LangSmith、Checkpoint 状态快照

**交付物：独立可 curl 调试的 LangGraph 导购服务，对接 Java 后端**

### 阶段 3：MaxKB ↔ LangGraph 集成（胶水层）

1. MaxKB 配置外部工具，指向 LangGraph 服务接口
2. 异常适配：统一捕获超时、服务异常，返回友好文案，不抛堆栈
3. 记忆打通：preference_memory 持久化；可选偏好摘要回传 MaxKB 会话上下文

> 
> 两套会话体系：MaxKB 负责对话历史；LangGraph checkpoint 负责流程状态快照

**交付物：端到端链路：用户在 MaxKB 聊天 → 导购 Agent 执行 → 返回结果**

### 阶段 4：测试、灰度、上线

1. 单元测试：适配器、Worker、护栏校验
2. LangSmith 链路核验：路由分支、数据校验、无编造
3. 灰度放量
4. Docker 打包 LangGraph 服务，和 MaxKB、Java 后端统一容器运维，配置健康检查

**交付物：可正式对外使用的导购系统**

### 阶段 5：进阶扩展（可选，不上线强制依赖）

1. 改造异步接口，支持 human-in-the-loop 人工断点
2. MaxKB 新增运营审核应用，通过 thread_id 恢复流程
3. 新增 Worker：比价、售后、订单查询
4. 参考 commerce‑agents 评估脚本，增加导购效果评估

## 三、关键约束 & 避坑清单

- ❌ 禁止修改 MaxKB 源码，使用外部 HTTP 工具接入，保障升级
- ❌ 不直接复用 commerce‑agents 源码，只借鉴架构范式（原生强绑定 Claude）
- ✅ 核心业务顺序用代码 ConditionalEdge 兜底，不依赖 LLM 记忆
- ✅ 护栏优先代码校验，而不是仅靠 prompt 约束
- ✅ 服务隔离：MaxKB / LangGraph 导购服务 / Java 后端独立部署、独立扩缩容
- ✅ 鉴权：MaxKB 负责用户鉴权；LangGraph 校验透传 token，防止非法调用

## 四、能力边界

✅ 拥有

- MaxKB 全套平台能力：聊天界面、RAG、权限、其他简易助手
- 可控导购 Agent：结构化状态、代码硬路由、多层防幻觉护栏、LangSmith 全链路追踪
- 可扩展：新增 Worker、子图、人工审批断点
- 兼容原有 Java 业务接口

❌ 不自带

- 监控告警（需自行接入 Prometheus/Grafana）
- LangGraph 侧多租户隔离（需要透传 tenant_id 额外开发）
- 自动评估测试套件（可参考 commerce‑agents 评估脚本改造）

## 五、上线推荐顺序

阶段 1 → 阶段 2（同步版本，不做断点） → 阶段 3 → 阶段 4，直接上线；阶段 5 迭代后置。




