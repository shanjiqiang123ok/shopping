# 护栏规则清单（GUARDRAILS）

> 移植 `commerce-agents` 的护栏范式（`docs/safety.md` 式的「规则 ↔ 模块」对照），全部用**代码**实现，不依赖 prompt 约束（`frame.md` 关键约束第 4 条）。
> 实现位置：`src/shopping_backend/guardrails/`，调用点：`agent_api`（前置）、`backends`（后置）、`graph/nodes/summarize`（溯源）。

---

## 一、护栏总览（7 层）

| 层 | 名称 | 位置 | 目标 | 失败动作 | 日志级别 |
| --- | --- | --- | --- | --- | --- |
| L1 | 调用方鉴权 | `agent_api/deps.verify_token` | 阻止非法调用（MaxKB 之外的直连） | 401，不进图 | WARN |
| L2 | 入参 Schema | `guardrails/validate_input.py` | 长度/字符集/范围 | 422 + 逐字段 `errors[]` | INFO |
| L3 | 工具/Worker 入参 | `validate_input.parse_goods_query` | 模型可控参数越界（limit、价格、品类） | 返回「参数非法」文本给模型，不抛异常 | WARN |
| L4 | 出参逐条 Schema | `validate_output.py` | 后端脏数据/字段缺失 | **逐条丢弃**，整批非法才置空 | WARN |
| L5 | 字段白名单 | `validate_output.py` | 未知字段污染 State | 落 State 前按 DTO 重新 `model_dump` | DEBUG |
| L6 | 文本 fence | `fencing.py` | 外部文本 prompt 注入 | 归一化 + 截断后使用 | DEBUG |
| L7 | 溯源/防幻觉 | `grounding.py` | 回答里的 ID/价格/券码无出处 | **剔除所在行**；全剔则回退纯模板 | ERROR→计数 |

顺序：`L1 → L2 → L3 → (调用后端) → L4 → L5 → (进入 State) → 渲染 → L7`。
L4/L5 的丢弃计数会累加进 `meta.degraded`，但**不**使请求失败（对齐 F7「后端失败不中断图」）。

---

## 二、L2/L3 入参规则明细

| 字段 | 规则 | 违反时 |
| --- | --- | --- |
| `query` | 1–2000 字符，strip 后非空；禁止 `<script`、NUL 字节 | 422 `QUERY_TOO_LONG` / `INVALID_ARGUMENT` |
| `user_id` `session_id` `tenant_id` | `^[A-Za-z0-9_\-]{1,64}$` | 422 |
| `preference_memory` | dict，键 ≤64、值 ≤200，条目 ≤32；值经 fence | 超上限截断，非法字符清洗 |
| `GoodsQuery.limit` | 1–20，`min(值, MAX_GOODS_LIMIT)` 钳制 | 钳制而非报错 |
| `GoodsQuery.min/max_price_cents` | ≥0；`min<=max`（否则交换） | 交换 + WARN |
| `GoodsQuery.query` | ≤200 | 422 / 截断 |
| `goods_ids` | 1–50 个，`[A-Za-z0-9_\-]{1,36}` | 422 |
| 身份类字段 | **禁止**出现在工具入参（`extra="forbid"`） | 422 + ERROR 日志 |

`extra="forbid"` 是 L3 的核心：模型若试图传 `user_id`/`is_admin`/`price_override` 之类字段，直接被 Schema 拒绝（commerce-agents 的「身份只从 session 取，模型永不见 token」范式）。

---

## 三、L4/L5 出参规则明细

对 `search_goods` / `get_stock` / `get_coupon` 三个返回点统一执行：

```
rows_in = 原始记录
for row in rows:
    try: dto = GoodsOut.model_validate(row)          # 类型/范围/枚举校验
    except ValidationError as e: dropped.append((row_id_or_index, e.errors())); continue
    if dto.status != "on_sale": dropped_status += 1; continue     # R-G1 二次兜底
    kept.append(dto.model_dump(mode="json"))                      # L5：重新序列化 = 白名单
if not kept and rows_in:
    degraded = True; error_msg = "商品信息校验未通过"
```

具体丢弃条件：

| DTO | 必校验 | 丢弃条件 |
| --- | --- | --- |
| `GoodsOut` | `id`/`sku`/`title` 非空、`price_cents >= 0` 整数、`status` 枚举 | 缺 `id`、价格为负或浮点串、`status != on_sale`、`title` 超 120 |
| `StockOut` | `available == max(on_hand-reserved, 0)`、`goods_id` 非空 | 库存记录内部矛盾（用计算值覆盖展示值，不丢行）；缺 `goods_id` 丢行 |
| `StockOut` 缺键 | 请求了但响应里没有该 ID | **补 `available=0` 空记录**（不丢、不猜），保证 R-S2 |
| `CouponOut` | `code` 非空、`discount_type` 枚举、`threshold_cents>=0`；**状态与有效期在护栏层二次拦截**（业务服务已过滤一次，面对第三方促销系统时不能只信一次） | 非 active / 已过期 / 未生效 → 丢弃并计数（`expired`、`not_started`、`status_*`） |
| `CouponOut.display` | 派生字段，不接受 wire 值 | 校验通过后由 `format_coupon_display` 统一重算（D9），避免 Java 侧漏传导致话术缺门槛 |
| 分桶校验 | 券按「来自哪个 goods_id 桶」归属，不按 code 回查 | 保证任何 driver（含字段被改名后）的归属关系都不会错（D10） |
| `CouponOut` 力度 | `percent` 时 `discount_value ∈ [1,999]`；`amount` 时 `>0` | 越界丢弃（防「0.1 折」脏数据进话术） |

一致性交叉校验（跨域，单条通过仍可能被整体拒）：
- `stock_map` 的 key 必须 ⊆ `goods_list` 的 id（多余 key 丢弃）。
- `coupon_map` 的 key 必须 ⊆ `seen_goods_ids`（防后端串号）。
- `coupon.threshold_cents > 商品 price_cents` 时不写入该商品的券列表（R-C 第 5 步的服务端兜底）。

---

## 四、L7 溯源与防幻觉（核心）

`seen_goods_ids` 是唯一事实白名单，由 `goods_worker` 在通过 L4/L5 后填充，随 checkpoint 持久化。

`grounding.verify(answer, state)` 逐行扫描：

| 检测 | 模式 | 判定 | 动作 |
| --- | --- | --- | --- |
| 未溯源商品 ID | `\bg_\d{1,6}\b` | 不在 `seen_goods_ids` | 剔除该行 |
| 未溯源 SKU | `\b[A-Z]{2,4}-[A-Z0-9]{2,8}\b` | 不在 `goods_list[].sku` | 剔除该行 |
| 未溯源券码 | `\b[A-Z][A-Z0-9_]{2,20}\b`（券码位） | 不在 `coupon_map` 值集 | 剔除该行 |
| 价格不符 | 行内 `¥\d+(\.\d{1,2})?` | 与该行所指商品的 `price_cents/100` 不等（±0.005） | 剔除该行 |
| 库存数不符 | 行内 `现货\s*(\d+)\s*件` | 与 `stock_map[id].available` 不等 | 剔除该行 |
| 编造数字 reason | LLM `reason` 内出现 `¥`/`\d+件`/`\d+折`/券码 | 命中即整条 `reason` 丢弃（O9） | 退回无 reason 模板 |
| 越权动作 | `\b(下单|已购买|付款|发货|锁定库存)\b` | 命中 | 替换为合规话术（O7） |

全部行被剔除时 → 回退到 `presentation.py` 的纯模板渲染（此时 `answer` 100% 服务端构造），并 `degraded=true`。

> 结构性保证优先于检测：`answer` 的**骨架与数字本来就由模板从 State 渲染**（ADR A6），LLM 只被允许填 `reason` 自由文案，因此上表是第二道防线，不是唯一防线。

---

## 五、fence 规则（L6）

对**所有**进入 prompt 的外部文本（`query`、`goods.description`、`coupon.name`、`preference` 值、MaxKB 回传的 `preference_memory`）：

1. `unicodedata.normalize("NFKC")`；
2. 移除控制字符（`C0/C1`）、零宽与双向字符（`U+200B–U+200F`、`U+202E`、`U+2066–U+2069`）；
3. 移除特殊 token 样式 `<\|[^|>]*\|>` 与嵌套伪造 `</label</label>>`（迭代到不动点）；
4. 中和角色伪造：行首 `system:` / `assistant:` / `user:` / `<tool_call>` 替换为 `[filtered]`；
5. 截断 `max_fenced_chars = 8000`，超长追加 `…[truncated]`；
6. 包裹标签时 **label 必须来自 `fencing.ALLOWED_LABELS` 白名单**（`agent_query`、`goods_desc`、`goods_row`、`coupon_row`、`preference_block`、`system_note`、`context_note`）。运行时拼出来的标签（`f"goods_{id}"`）直接 `ValueError`，这样"标签是源码字面量"这条规则由代码保证而不是靠 review 记性。

---

## 六、偏好写入过滤

`services/preference.py` 落库前执行：

| 规则 | 模式 | 动作 |
| --- | --- | --- |
| 疑似卡号/长数字串 | `\d{9,}`（含空格分隔） | 拒绝落库 |
| IBAN 样式 | `\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b` | 拒绝 |
| 邮箱 | `\S+@\S+\.\S+` | 拒绝 |
| 手机号（中国大陆） | `\b1[3-9]\d{9}\b` | 拒绝 |
| 键名规范 | 小写 + 下划线，≤64 | 归一化后落库 |
| 值长度 | ≤200 | 截断 |
| 去重 | 归一化 containment / Jaccard ≥ 0.6 | 视为同一条，upsert |
| 单轮写入上限 | 3 条 | 超限丢弃尾部 |

拒绝消息**不回显原值**（只说「含敏感信息，未保存」）；写入异常一律 catch + WARN（R-P4）。

---

## 七、鉴权与网络边界

| 面 | 机制 |
| --- | --- |
| MaxKB → Agent | `Authorization: Bearer ${AGENT_INTERNAL_TOKEN}`；常量时间比较（`hmac.compare_digest`）；token 为空则跳过校验 + 启动 WARN（仅 dev） |
| Agent → 业务后端 | `X-Internal-Token: ${BUSINESS_API_TOKEN}`，**每请求下发**而非只写在 client 上：注入的 client（测试、共享连接池）曾能绕过该凭证与 `BUSINESS_API_TIMEOUT_S`（D7）。`http` driver 默认 `127.0.0.1`，非本机需显式配 `BUSINESS_API_BASE_URL` |
| 超时预算 | `BUSINESS_API_TIMEOUT_S=5`、`LLM_TIMEOUT_S=20`、单请求总预算 `AGENT_TOTAL_TIMEOUT_S=30`（超限 → 降级话术） |
| 重试 | 幂等只读接口才重试；最多 `BUSINESS_API_MAX_RETRY=2`；指数退避 0.2s 起 |
| 日志脱敏 | query 超 120 字符截断；不打印 token；`preference` 值按 §6 模式打码 |

---

## 八、如何新增或放宽一条规则

1. 在本文件对应层加一行（规则 + 失败动作 + 期望日志级别）；
2. 在 `tests/test_guardrails.py` 加**正反两个**用例（有正向必须有对应负向，沿用 commerce-agents 的案例编写规则）；
3. 规则参数进 `Settings`（不要硬编码阈值），命名如 `MAX_FENCED_CHARS`、`GROUNDING_PRICE_TOLERANCE`；
4. 若涉及业务口径变化，同步改 `docs/SHOPPING_RULES.md`（口径唯一来源）；
5. 放宽规则必须留「为何放宽」的注释一行，否则 review 拒收。

**禁止的放宽方式**：把 L4/L7 从「代码校验」降级为「prompt 提示模型不要编造」——这直接违反 `frame.md` 关键约束。

---

## 九、单测清单（已实现）

规则↔用例的对应关系全部落成代码，`uv run pytest -q` 一条命令跑完（无需网络与模型 Key）：

| 文件 | 覆盖 | 用例数 |
| --- | --- | --- |
| `tests/test_db_engine.py` | 方言中立 9 条规则、PRAGMA、PK/金额/类型断言 | 17 |
| `tests/test_services.py` | R-S1/R-S2、券六步、偏好 tier-1 与写过滤 | 27 |
| `tests/test_guardrails.py` | L2–L7 逐条（含 fence 注入、grounding 剔除） | 38 |
| `tests/test_backends.py` | 三 driver 契约矩阵、超时/重试/降级、`http_java` 映射 | 23 |
| `tests/test_routing.py` | F1–F4 + `resolve_only` + F6 预算 | 17 |
| `tests/test_supervisor.py` | 意图、槽位、虚词、序数、`不要超过` 修正 | 41 |
| `tests/test_graph_e2e.py` | §9 用例 1–8、`route_trace` 断言、grounding 零丢弃 | 15 |
| `tests/test_checkpoint.py` | SQLite 重开、线程隔离、`thread_id` 必填 | 7 |
| `tests/test_business_api.py` | §3 契约、R-S2 补零、401/422 | 14 |
| `tests/test_agent_api.py` | §2 契约、token、日志落库、偏好回写、降级、`/chat` 页面与对外契约隔离 | 19 |
| `tests/test_alembic.py` | 迁移与 metadata 不漂移 | 4 |

下面这张表是**规则维度**的原始清单（保留作为 review 对照，右列标注落在哪个文件）：

| 用例 | 断言 |
| --- | --- |
| `test_goods_query_forbid_extra_identity` | 传 `user_id` 进 `GoodsQuery` → 校验失败 |
| `test_limit_clamped` | `limit=999` → 实际 20 |
| `test_price_min_gt_max_swapped` | 交换且记 WARN |
| `test_drops_unknown_field_from_state` | 后端多返回 `secret_score` → 不进 State |
| `test_drops_negative_price_row_keeps_others` | 脏行丢弃、干净行保留 |
| `test_status_off_sale_filtered_even_if_backend_wrong` | L4 二次兜底 |
| `test_stock_missing_key_becomes_zero_not_absent` | R-S2 |
| `test_expired_coupon_dropped` / `test_paused_coupon_dropped` | §三 |
| `test_fence_strips_role_forgery_and_bidi` | §五 |
| `test_grounding_rejects_invented_goods_id` | L7 |
| `test_grounding_rejects_price_mismatch_line` | L7 |
| `test_grounding_drops_reason_with_numbers` | O9 |
| `test_preference_blocks_card_like_number` | §六 |
| `test_backend_never_raises_on_timeout` | F7，返回 `degraded` 而非异常 |
| `test_three_drivers_same_contract_matrix` | 脏数据/超时/5xx/缺键四场景 × 三 driver 等价 |
