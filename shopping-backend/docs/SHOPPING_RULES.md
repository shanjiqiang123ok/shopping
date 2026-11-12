# 导购业务规范（Shopping Rules）

> 本文件是 `frame.md` **阶段 1 的交付物之一**：「导购业务规范文档」。
> 它先于代码定义业务口径，是 Worker 实现、护栏规则、输出文案、测试用例的唯一事实来源。
> 代码里的路由条件（`graph/routing.py`）、校验白名单（`guardrails/*`）、话术模板（`graph/nodes/presentation.py`）必须与本文件一致。

---

## 一、业务范围与角色

| 项 | 约定 |
| --- | --- |
| 场景 | 电商站内导购问答：推荐选品、查库存、查优惠 |
| 用户 | C 端消费者（MaxKB 侧登录用户），以 `user_id` 透传 |
| 租户 | 单租户起步，`tenant_id` 预留字段（默认 `default`），LangGraph 侧不做租户隔离 |
| Agent 能力边界 | **只查询与推荐，绝不下单、绝不加购、绝不承诺发货时效与价格锁定** |
| 数据事实来源 | 商品/库存/优惠券 100% 来自业务后端；模型不得生成任何业务字段 |

三个业务域：

```
商品域 goods   ——  搜索、筛选、详情、上下架状态
库存域 stock  ——  多仓可售量（available = on_hand - reserved）
营销域 coupon ——  券的适用范围、门槛、折扣、有效期、券库存、每人限用
```

---

## 二、数据字典与口径

### 2.1 商品 goods

| 字段 | 类型 | 口径 |
| --- | --- | --- |
| `id` | str(36) | 服务端生成的 UUID 字符串。**跨方言中立**，不用自增/AUTO_INCREMENT |
| `sku` | str(64) | 商家可见编码，展示与溯源用；大写形式参与防幻觉比对 |
| `title` | str(120) | 展示标题，唯一可出现在话术里的商品名 |
| `category` | str(32) | 品类，检索主维度；取值见 §2.6 品类表 |
| `brand` | str(32) | 品牌，可空串 |
| `description` | text | 卖点长文本，进入 prompt 前**必须 fence 清洗**（外部内容） |
| `price_cents` | int | **一律以「分」为单位**，禁止浮点。展示层格式化 |
| `currency` | char(3) | 默认 `CNY` |
| `tags` | text(JSON) | 标签数组，JSON 字符串存储（不用方言专属 JSON 类型） |
| `status` | str(16) | `on_sale` \| `off_sale` \| `draft`。**只有 `on_sale` 可被推荐** |
| `created_at` / `updated_at` | datetime | naive UTC，展示层再转时区 |

业务规则：
- R-G1 搜索默认只返回 `status=on_sale`；`off_sale`/`draft` 不进入候选，也不参与库存与券查询。
- R-G2 单次搜索返回条数上限 `MAX_GOODS_LIMIT=20`，话术展示上限 6 条（见 §6）。
- R-G3 价格筛选语义：`min_price_cents <= price <= max_price_cents`，含端点；「2000 元以内」解析为 `max_price_cents=200000`。
- R-G4 商品标题/描述属外部文本，进入 prompt 前经 fence：NFKC 归一、去控制与双向字符、中和 `system:`/`assistant:` 角色伪造、截断 8000 字符。

### 2.2 库存 stock

| 字段 | 类型 | 口径 |
| --- | --- | --- |
| `goods_id` | str(36) FK | 关联 goods.id |
| `warehouse` | str(32) | 仓库编码，`UNIQUE(goods_id, warehouse)` |
| `on_hand` | int | 在库量 |
| `reserved` | int | 已占用（未发货订单锁定）量 |
| `available` | **计算值，不落库** | `max(on_hand - reserved, 0)`，多仓时**按商品汇总求和** |

业务规则：
- R-S1 可售量口径唯一：`available = sum_over_warehouses(max(on_hand - reserved, 0))`；负数按 0 处理。
- R-S2 **无库存记录 = 不可售**（`available=0`），而不是「数据缺失」。缺记录时话术走缺货分支。
- R-S3 `available > 0` 定义为有现货；`== 0` 定义为缺货，触发优惠券分支（见 §5）。
- R-S4 缺货商品的券推荐仍然给出（引导替代/等待补货），但话术必须显式说明「当前无现货」。
- R-S5 禁止把 `on_hand` 直接当可售展示（防超卖话术）。

### 2.3 优惠券 coupon

| 字段 | 类型 | 口径 |
| --- | --- | --- |
| `id` / `code` | str | `code` 唯一，券码可展示（`M1500_100` 这类可读码） |
| `name` | str(64) | 券名，展示用 |
| `scope` | str(16) | `all` \| `category` \| `goods` |
| `scope_value` | text | 逗号分隔：category 值或 goods_id 列表；`all` 时为空 |
| `threshold_cents` | int | 使用门槛（订单/商品价 ≥ 门槛才可用） |
| `discount_type` | str(16) | `percent` \| `amount` \| `shipping` |
| `discount_value` | int | `amount`→分；`percent`→千分比（900 = 9 折，即减 10%）；`shipping`→忽略值 |
| `valid_from` / `valid_to` | datetime | naive UTC；有效性以**服务端时间**判定，不信模型 |
| `total_quota` / `used_count` | int | 券库存；`total_quota=-1` 表示不限量 |
| `per_user_limit` | int | 每人限领次数（>0 时参与可领判断） |
| `status` | str(16) | `active` \| `paused` \| `expired` |

业务规则（券可用性判定顺序，全部在服务端 `services/coupon.py` 完成）：

```
1. status == active
2. valid_from <= now_utc <= valid_to        否则归为不可用（expired）
3. total_quota < 0 或 used_count < total_quota  否则「已抢完」，不推荐
4. 适用范围匹配：
   scope=all      → 对任意商品可用
   scope=category → 商品 category ∈ scope_value
   scope=goods    → goods_id ∈ scope_value
5. 门槛匹配：price_cents >= threshold_cents
   （scope=goods 且命中多商品时，取该商品价比对；scope=all/category 时取候选商品最高价比对）
6. 排序：命中商品的券按「优惠力度从大到小」→「threshold_cents 从低到高」→「valid_to 从近到远」
```

- R-C1 力度换算统一折算为「分」用于排序：`amount` 直接取值；`percent` 取 `round(price * (1000-v)/1000)`；`shipping` 取固定权重 `SHIPPING_WEIGHT_CENTS = 1000`（¥10 演示口径）。
  > 落地修正：原方案想「排在 amount 之后、percent 之前」，但那样 ¥20 的耳机券会被免邮挤掉，用例 3 就拿不到有意义的满减话术了。改为固定小权重，见 `services/coupon.py`。
- R-C2 **展示必带门槛**：`满¥1500.00减¥100.00（券码 M1500_100）`，缺门槛的券不展示，避免用户误以为无门槛；门槛为 0 时展示 `立减¥50.00` / `9折` / `免邮`。`display` 是派生字段，由服务端在校验后重算，wire 值不采信（D9）。
- R-C3 每人限用/已抢完属服务端事实，模型不得改写数值。
- R-C4 无 goods 上下文（纯「有什么满减活动」）时，返回全部 `scope=all` 且有效的券，上限 8 条。
- R-C5 叠加策略本阶段**不实现**（无最优券组合计算），每个商品只展示力度最大的 1 张可用券；扩展位见 §8。

### 2.4 用户偏好 user_preference

| 字段 | 口径 |
| --- | --- |
| `(user_id, pref_key)` | 复合主键 |
| `pref_value` | 长度 ≤ 200 |
| `category` | `preference` \| `constraint` \| `context` |
| `source_session_id` | 每条偏好必须有来源会话（溯源） |

- R-P1 偏好为**只读注入**：Agent 可读、可在轮次结束后写，但业务后端不接受「模型直接改偏好」的入参。
- R-P2 `constraint`（预算、禁忌、地域）优先注入，其后按 `updated_at` 倒序，总数上限 8 条（tier-1 自动注入）。
- R-P3 **写入过滤**：命中 9 位以上连续数字、IBAN 样式、邮箱、卡号后四位等模式一律拒绝落库；拒绝消息**不回显原值**。
- R-P4 偏好写失败绝不使本轮失败（catch + WARN），最多降级为不记忆。
- R-P5 用户清除偏好后，同一轮的抽取任务须丢弃（purge generation 校验，M7 实现）。

### 2.5 金额与时间

- 所有金额：`*_cents` 整数分。展示格式化：`¥1699.00`（`cents/100`，两位小数，整元也保留 `.00`）。
- 所有时间：**timezone-aware UTC**（`datetime.now(UTC)`），比较一律先经 `ensure_utc()` 归一；序列化后形如 `2026-12-11T15:59:59Z`。展示层再转时区。
  > 落地修正：原方案写的是 naive UTC，但 SQLModel 的 `UTCDateTime` 列类型会拒绝 naive 值，且带偏移写入各方言更不容易被服务器时区影响。见 `docs/ARCHITECTURE.md` D3。
- 有效性判定使用**服务端注入的时间**（`ShoppingSessionContext.now`），不接受客户端或模型传时间。

### 2.6 演示品类表（种子数据用）

`phone`（手机）、`earphone`（耳机）、`tablet`（平板）、`laptop`（笔记本）、`accessory`（配件）

---

## 三、意图分类与槽位抽取

Supervisor 输出必须是结构化的 `SupervisorDecision`，`intent` 严格落在枚举内：

| intent | 触发语义 | 必带槽位 | 下游节点链 |
| --- | --- | --- | --- |
| `goods_query` | 推荐、选品、有什么…、看看、对比 | `category?`, `max_price_cents?` | goods → stock →(有货)→ summarize |
| `stock_query` | 有货吗、现货、库存、还剩几件、有卖 | `goods_ref`（商品名或 sku） | goods(仅用于**解析商品 ID**) → stock → summarize |
| `coupon_query` | 有券吗、优惠券、满减、活动、能便宜多少 | `goods_ref?` | coupon → summarize |
| `composite` | 同时含选品条件 + 库存/优惠诉求 | 上述并集 | goods → stock →(全缺货或有券意图)→ coupon → summarize |
| `other` | 与导购无关、信息不足 | — | summarize（澄清话术） |

槽位抽取规则（`rule_based` 模式的确定性实现，`llm` 模式的 prompt 也照此约束）：

| 槽位 | 抽取模式 | 示例 |
| --- | --- | --- |
| `max_price_cents` | `(\d+)\s*(元|块|rmb)?\s*(以内|以下|不超过|最多|预算)` | 「2000 元以内」→ 200000 |
| `min_price_cents` | `(超过|以上|高于)\s*(\d+)` | 「3000 以上」→ 300000 |
| `category` | 品类词表同义词映射（见下表） | 「买手机」→ `phone` |
| `goods_ref` | 品类词外的实体片段，长度 2–40，去标点 | 「红米 note13 pro 有货吗」→ `红米 note13 pro` |
| `coupon_needed` | 券/优惠/满减/活动/省钱 关键词 | — |

品类同义词表：

```
phone     : 手机, 智能机, iphone, 安卓机, 拍照手机
earphone  : 耳机, 蓝牙耳机, 降噪耳机, 耳夹, tws
tablet    : 平板, 平板电脑, ipad
laptop    : 笔记本, 电脑, 轻薄本, 游戏本
accessory : 配件, 充电器, 数据线, 保护壳, 充电宝
```

冲突处理优先级：
1. 显式 `intent` 关键词（券 > 库存 > 选品）优先决定主分支；
2. 同时命中「库存 + 券」→ `composite`；
3. 命中品类但无价格 → 不限价，按销量/默认序返回；
4. 无任何槽位且无品类 → `other`（澄清），**不允许猜测成 goods_query 全量返回**。

> 关键约束（防幻觉）：`stock_query` 的 `goods_ref` 是**商品名**，不是商品 ID。Agent **禁止**把名称直接当 `goods_id` 传给库存接口，必须先经 `goods_worker` 搜索解析出真实 ID 与 `seen_goods_ids`，再查库存。这条规则保证「先商品后库存」的顺序由代码兜底，也避免凭空造 ID（见 `docs/GUARDRAILS.md` §4）。

---

## 四、流程硬约束（必须代码实现，不依赖模型）

| 编号 | 规则 | 实现位置 |
| --- | --- | --- |
| F1 | 商品搜索先于库存查询；`goods_worker` 有候选时**必然**进入 `stock_worker`（无候选时不必再查库存，直接汇总澄清） | `routing.route_after_goods` + `builder.py` ConditionalEdge |
| F2 | 只要存在 `available > 0` 的商品 → 直接 `summarize`（不额外查券，除非 `composite` 且 `coupon_needed=true`） | `routing.route_after_stock` |
| F3 | 全部候选商品 `available == 0` → 必走 `coupon_worker` | 同上 |
| F4 | `coupon_worker` 完成后**无条件**进入 `summarize` | `add_edge` |
| F5 | `summarize` 只读已通过后置护栏的 State 字段，不发起任何新的后端调用 | `nodes/summarize.py` |
| F6 | 单次请求 Worker 调用次数上限 4（goods/stock/coupon/summarize 各一次），超限直接 `summarize` 并记 `degraded` | `builder.py` 递归上限 + state 计数 |
| F7 | 任何 Backend 失败不得中断图执行，必须归一为 `error_msg` 后继续到 `summarize` | `backends/base.py`（永不抛异常契约） |
| F8 | LLM 只被允许产出 `SupervisorDecision`（枚举 intent + 槽位）和每条推荐的 `reason` 文案；其余业务字段一律来自后端 | `supervisor.py` / `presentation.py` |

### 状态分支矩阵（验收依据）

| 输入形态 | 有货 | 需券意图 | 实际链路 |
| --- | --- | --- | --- |
| 选品 | 是 | 否 | `supervisor → goods → stock → summarize` |
| 选品 | 是 | 是（composite） | `supervisor → goods → stock → coupon → summarize` |
| 选品 | 全否 | 否 | `supervisor → goods → stock → coupon → summarize` |
| 选品 | 全否 | 是 | 同上（只查一次券） |
| 库存指代 | — | — | `supervisor → goods(解析ID) → stock → summarize` |
| 纯券 | — | — | `supervisor → coupon → summarize` |
| 无结果 | — | — | `supervisor → goods(空) → summarize`（澄清话术，不编造） |
| other | — | — | `supervisor → summarize` |

---

## 五、话术与输出规范

### 5.1 结构

回答由服务端模板渲染，模型只填 `reason`：

```
① 结论句（1 行）：命中数量 + 主限定条件复述（条件不带 ¥，见下方记法）
② 商品条目（1–6 条，相关性降序、同分按价格升序）：
   {序号}) {title} ¥{price}｜{库存状态}｜{品牌}
           理由：{reason（模型生成，≤40 字，仅描述需求匹配，不得含数字型业务字段）}
③ 优惠信息（存在可用券时，同一张券合并成一行并列出适用商品）：
   可用券：{display}（券码 {code}，{valid_to:%Y-%m-%d} 前有效）｜适用：{title1}、{title2}
④ 结尾引导（1 行）：可继续追问的方向（比价/补货/其他品类）
```

**记法约定（重要）**：`¥` 前缀的数字**只用于**来自 State 的业务事实，会被 L7 溯源校验；复述用户原话的限定条件（如「2000 元以内」）不带 `¥`，因此不会被误判成编造价格。校验侧与被校验侧共用 `domain/formatting.py`，两边不可能对「¥1699.00 代表什么」产生分歧。

### 5.2 硬性规则

| 编号 | 规则 |
| --- | --- |
| O1 | 展示商品条数 1–6；命中 >6 时取前 6 并注明「另有 N 款可选」 |
| O2 | 价格必须带 `¥` 且与 `price_cents/100` 完全一致；不一致的整行剔除 |
| O3 | 库存状态只允许 4 种表述：`现货{n}件` / `仅{n}件（紧张）`（1–3）/ `无现货` / `已下架`（后者不展示） |
| O4 | 券必须同时展示门槛与力度；`percent` 券展示为「9 折」而非千分比 |
| O5 | 缺货场景必须先说明缺货、再给券/替代，**不得**先说优惠掩盖缺货 |
| O6 | 无任何命中时使用澄清话术，不输出商品条目，不猜测型号 |
| O7 | 禁止出现：下单链接拼接、订单号、发货时效承诺、价格锁定承诺、库存「一定还有」类断言 |
| O8 | 降级话术（后端失败/超时）：`暂时无法获取商品信息，请稍后再试或换个说法。` + `meta.degraded=true`，HTTP 仍 200 |
| O9 | `reason` 中若出现价格、库存数、券码（正则可检），该行 `reason` 整体丢弃，退回无 reason 版模板 |

### 5.3 话术模板表（`presentation.py` 实现依据）

| 场景 | 模板（`presentation.py` 实际输出） |
| --- | --- |
| 选品有货 | `为你找到 {n} 款{condition}在售商品：` + 条目 + 券行 + `{closing}`；条件以数字开头时前面补一个空格（`款 2000 元以内…`），纯品类不补（`款耳机…`） |
| 选品全缺货 | `这 {n} 款{condition}商品当前均无现货，可先领可用券，补货后再下单：` + 条目 + 券行 |
| 库存查询（resolve_only） | `{title} ¥{price}：{库存状态}` + `分仓情况：{wh} 可售{n}件、…` + 可用券行（若有） |
| 纯券 | `当前可参与的优惠：` + 券行（≤8） |
| 无商品命中但有券（composite） | 走「无命中」澄清，不给券（对空候选发券是误导） |
| 无命中 | `没找到符合「{query[:40]}」的在售商品，可以换个品类或放宽预算再问我。` |
| other | `我可以帮你查商品、库存和优惠券，你想看哪一类？` |
| 交易类请求（O7 拒绝） | `这里只能帮你查商品、库存和优惠券，不能替你下单或付款。要看这款的现货情况和能用的券吗？` |
| 后端不可达（O8） | `暂时无法获取商品信息，请稍后再试或换个说法。`；编程错误则为 `系统繁忙，请稍后再试。`（两者刻意区分，避免把 bug 说成数据故障） |
| 序号越界 | `上一轮只有 {n} 款候选，没有第 {ordinal} 款。` |

---

## 六、护栏业务口径（与 GUARDRAILS.md 对应）

| 护栏点 | 业务口径 |
| --- | --- |
| 前置入参 | `query` 长度 1–2000；`user_id`/`session_id` 字符集 `[A-Za-z0-9_\-]` 且 ≤64；`limit ≤ 20`；价格区间非负且 `min <= max` |
| 后置出参 | 每条记录必须通过 `GoodsOut/StockOut/CouponOut` 校验；**逐条丢弃**非法项，整批仅在全部非法时置空并记 `degraded` |
| 字段白名单 | 只有 DTO 声明字段能进 State；后端多返回的未知字段一律不落 State |
| 状态一致 | `status != on_sale` 的商品不进入 `goods_list`（即使后端误返回）；`available<0` 归 0 |
| 溯源 | 回答中出现的 sku / 商品 ID / 券码 / 价格必须能在 State 中找到；找不到则剔除所在行 |
| 幂等 | 本阶段全部为只读操作，无写业务，天然幂等；写类操作（加购）落地时必须复用 `seen_goods_ids` 白名单 |

---

## 七、演示数据要求（种子规范）

| 数据集 | 最小规模 | 必备边界样本 |
| --- | --- | --- |
| goods | 12 条，覆盖 5 个品类 | ①3 条 ≤2000 元手机（验收用例 1）②1 条 `off_sale`（须被过滤）③1 条价格恰好 200000 分（边界含端点）④1 条价格 200001 分（不可命中「2000 以内」） |
| stock | 每商品 1–2 仓 | ①1 款多仓汇总（on_hand 8 + 5，reserved 3 → available 10）②1 款 `available=0`（触发 F3 券分支）③1 款 `reserved > on_hand`（负数归 0）④1 款**无库存记录**（R-S2 视为 0） |
| coupon | 8 条，三种 scope | ①`percent` 9 折 ②`amount` 满减 ③`shipping` 免邮 ①1 条 `status=paused`（须过滤）②1 条 `valid_to < now`（须过滤）③1 条 `used_count == total_quota`（已抢完）④1 条门槛高于目标商品价（不可用） |
| preference | 2 用户 × 3 条 | 含 1 条 `constraint`（预算）验证 tier-1 优先注入；1 条含疑似卡号（验证 R-P3 拒绝落库） |

---

## 八、扩展位（本阶段不实现，口径先定）

| 扩展 | 预留方式 |
| --- | --- |
| 最优券组合/叠加计算 | `services/coupon.py` 的 `_STRATEGY_COUNTS = {"single_best": 1}` 是扩展点：新增 `"stacking": N` 即改变每商品展示券张数，调用点 `CouponService.pick(goods_ids, strategy=...)` 不变；未登记的 strategy 记 `coupon_strategy_unimplemented` 日志并回退 `single_best`（不静默叠加）。每人限用判定需要「用户领券记录」表，尚未实现，因此券查询接口**不接受 `user_id`**（见 `docs/API.md` §3.4） |
| 加购/下单 | `StorefrontBackend` 增加 `add_to_cart` 等写方法，强制走 `seen_goods_ids` 溯源 + `X-Internal-Token` + 幂等键（由 `session_id` 派生） |
| 补货订阅 | 缺货分支已产出结构化 `out_of_stock_ids`，可直接接订阅 Worker |
| 比价/售后/订单 Worker（阶段 5） | `builder.py` 的节点注册表与 `routing` 表结构支持新增分支，无需改现有边 |
| 人工断点 HITL | `summarize` 前留 `awaiting_approval` 状态位与 `interrupt()` 挂点；`thread_id` 已在 config 中，恢复流程用 `Command(resume=...)` |
| 效果评估 | 参考 commerce-agents 的 case schema（`calls_tool` / `never_calls` / `reply_omits` / `cart_contains` 变体 / `rubric`），M7 建 `evals/` |

---

## 九、验收用例（业务口径版）

| # | query | 期望链路 | 期望输出要点 |
| --- | --- | --- | --- |
| 1 | 推荐 2000 元以内的手机 | supervisor → goods → stock(有货) → summarize | 3 款 ≤¥2000 在售手机（`g_0001/g_0002/g_0003`，`g_0004` 的 ¥2000.01 与 `g_0005` 的 off_sale 均不出现）；**不含** coupon_worker；价格与 `price_cents` 一致 |
| 2 | Redmi Note 13 Pro 有货吗 | supervisor → goods(解析ID) → stock → summarize | 先解析出真实 ID（`seen_goods_ids` 非空、`resolve_only=true`），再给 `现货12件`；不编造 ID；**不**顺带发券 |
| 3 | 这款卖断了的耳机有优惠券吗 | supervisor → goods → stock → coupon → summarize | `intent=composite`；缺货商品行内标注 `无现货`（O5 在有货混合场景下的落地形式），券行带门槛与券码 `M199_20` |
| 4 | 有什么满减活动 | supervisor → coupon → summarize | 只返回 `scope=all` 有效券（`M5000_600`、`FREESHIP`），paused/expired/抢完进入 `unavailable` |
| 5 | 推荐 5000 元以内的电视 | supervisor → goods(空) → summarize | 澄清话术，**无商品条目**、不硬凑、无 `¥` |
| 6 | （业务后端不可达）推荐手机 | supervisor → goods(失败) → summarize | 降级话术 + `meta.degraded=true`，HTTP 200，无堆栈 |
| 7 | 同 session 连问「推荐 2000 元以内的手机」→「第二款有货吗」 | checkpoint 复用 | 第二问 `goods_list` 只含第一轮按价格排序的第二款；`route_trace` 恰好 4 步（证明未跨轮累加） |
| 8 | 帮我下单买第一个 | supervisor → summarize | 明确拒绝交易类请求（O7），不产生任何 goods 查询 |

以上 8 条中，1–4、6–7 由 `scripts/run_demo.py` 在**真实进程**上自动校验（`7/7 通过`，含用例 6 的 token 401 检查），8 与其余分支矩阵由 `tests/test_graph_e2e.py`、`tests/test_routing.py` 覆盖。
