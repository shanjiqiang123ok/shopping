# 接口契约（API）

> 两个服务：Agent 服务 :8000（给 MaxKB 调用）、业务后端服务 :8001（充当 Java 后端）。
> 本文件是 M2/M5 编码依据，也是 `docs/MAXKB_INTEGRATION.md` 里外部工具配置的来源。
> 真实 OpenAPI JSON 由 FastAPI 自动生成：`http://127.0.0.1:8000/openapi.json`、`http://127.0.0.1:8001/openapi.json`。

---

## 一、通用约定

| 项 | 约定 |
| --- | --- |
| 编码 | UTF-8，`Content-Type: application/json` |
| 金额 | 一律 `*_cents` 整数分；响应里**不返回**浮点价格 |
| 时间 | **timezone-aware UTC**，序列化为带 `Z` 的 ISO-8601（`2026-12-31T15:59:59Z`）。SQLModel 的 `UTCDateTime` 拒绝 naive 值，见 `docs/ARCHITECTURE.md` D3 |
| 请求追踪 | 响应含 `request_id`、`trace_id`；响应头 `X-Trace-Id` 与 `X-Elapsed-Ms`；可用 `X-Request-Id`/`X-Trace-Id` 传入复用 |
| 鉴权（:8000） | `Authorization: Bearer ${AGENT_INTERNAL_TOKEN}`，或 `X-Internal-Token`。缺失/不匹配 → **401，且不进入图**（有用例钉桩） |
| 鉴权（:8001） | `X-Internal-Token`；`BUSINESS_API_TOKEN` 为空时跳过校验并 WARN（本机默认） |
| 错误返回体 | `{"detail": "友好文案", "error_code": "MACHINE_CODE", "request_id": "..."}`，**无堆栈** |
| 限流 | 本阶段未实现（不引入 Redis/slowapi）。业务量控制靠 `MAX_GOODS_LIMIT`、`AGENT_TOTAL_TIMEOUT_S`、`recursion_limit` |

错误码表（两个服务共用）：

| HTTP | error_code | 含义 | 触发点 |
| --- | --- | --- | --- |
| 401 | `UNAUTHORIZED` | token 缺失或不匹配 | `agent_api/deps.verify_token` |
| 422 | `INVALID_ARGUMENT` | 入参 Schema 校验失败（含逐字段 `errors[]`） | `guardrails.validate_input` |
| 422 | `QUERY_TOO_LONG` | query 超 2000 字符 | 同上 |
| 404 | `GOODS_NOT_FOUND` | 商品 ID 不存在 | `business_api` |
| 503 | `DB_UNAVAILABLE` | 业务库不可用 | `/health` 检查失败 |
| 200 | `DEGRADED`（只出现在 `meta`） | 后端超时/护栏整批丢弃/LLM 回落 | 见 §2.3 |

---

## 二、Agent 服务 :8000

### 2.1 `POST /api/shopping-agent/run`

**请求**

```json
{
  "user_id": "u_1001",
  "session_id": "s_2026_11_11_001",
  "tenant_id": "default",
  "query": "推荐 2000 元以内的手机，有券吗",
  "preference_memory": {"budget": "2000元以内", "avoid_brand": "无"}
}
```

| 字段 | 必填 | 约束 |
| --- | --- | --- |
| `user_id` | ✅ | `[A-Za-z0-9_\-]{1,64}` |
| `session_id` | ✅ | `[A-Za-z0-9_\-]{1,64}`，同时作为 checkpoint `thread_id` 的组成部分 |
| `tenant_id` | ❌ | 默认 `default`，预留多租户命名空间 |
| `query` | ✅ | 1–2000 字符，进入 fence 清洗 |
| `preference_memory` | ❌ | dict[str,str]，MaxKB 侧偏好摘要回传用；服务端会与自己库里的 tier-1 偏好**合并**（服务端优先） |

**成功响应 200**

```json
{
  "request_id": "req_3f0c1a9b...",
  "trace_id": "t_c956065000",
  "answer": "为你找到 2 款耳机在售商品：\n1) QCY T13 真无线蓝牙耳机 ¥199.00｜无现货｜QCY\n2) 华为 FreeBuds Pro 3 耳机 ¥999.00｜现货28件｜华为\n可用券：满¥199.00减¥20.00（券码 M199_20，2026-12-31 前有效）｜适用：QCY T13 真无线蓝牙耳机、华为 FreeBuds Pro 3 耳机\n想只看有货的、或者换个预算再筛一次，告诉我就行。",
  "data": {
    "intent": "composite",
    "goods": [
      {"id": "g_0006", "sku": "QC-T13", "title": "QCY T13 真无线蓝牙耳机", "price_cents": 19900,
       "currency": "CNY", "category": "earphone", "brand": "QCY", "available": 0,
       "coupon_codes": ["M199_20"], "reason": ""},
      {"id": "g_0007", "sku": "HW-FBP3", "title": "华为 FreeBuds Pro 3 耳机", "price_cents": 99900,
       "currency": "CNY", "category": "earphone", "brand": "华为", "available": 28,
       "coupon_codes": ["M199_20"], "reason": ""}
    ],
    "stock": {
      "g_0006": {"goods_id": "g_0006", "on_hand": 6, "reserved": 6, "available": 0,
                 "warehouses": [{"warehouse": "WH-NJ", "on_hand": 6, "reserved": 6, "available": 0}]},
      "g_0007": {"goods_id": "g_0007", "on_hand": 30, "reserved": 2, "available": 28,
                 "warehouses": [{"warehouse": "WH-SH", "on_hand": 30, "reserved": 2, "available": 28}]}
    },
    "coupons": {
      "g_0006": [{"id": "c_0003", "code": "M199_20", "name": "耳机满199减20", "scope": "category",
                  "scope_value": ["earphone"], "threshold_cents": 19900, "discount_type": "amount",
                  "discount_value": 2000, "valid_from": "2026-11-01T00:00:00Z",
                  "valid_to": "2026-12-31T15:59:59Z", "total_quota": -1, "remaining_quota": null,
                  "per_user_limit": 2, "status": "active", "display": "满¥199.00减¥20.00"}]
    },
    "out_of_stock_ids": ["g_0006"]
  },
  "meta": {
    "mode": "rule_based",
    "backend": "inprocess",
    "route_trace": ["supervisor", "goods_worker", "stock_worker", "coupon_worker", "summarize"],
    "seen_goods_ids": ["g_0006", "g_0007"],
    "counts": {"goods": 2, "stock": 2, "coupons": 2, "dropped_rows": 0, "grounding_drops": 0},
    "degraded": false,
    "error_msg": null,
    "latency_ms": 19,
    "thread_id": "default:s_demo_3",
    "preference_summary": "avoid_brand=不考虑苹果生态；budget_limit=预算不超过 2000.00 元；preferred_category=主要看耳机"
  }
}
```

要点：
- `answer` 由服务端模板渲染（数字均来自 `data`），MaxKB 可直接展示；`data` 供前端卡片化渲染或二次加工。
- **记法约定**：带 `¥` 的数字一定来自 State（会被 L7 校验）；复述用户原话的「2000 元以内」不带 `¥`，因此澄清话术不会被误判为编造价格。
- `display` 是**派生字段**，由适配器在校验后统一重算，wire 里带的值不采信（`docs/ARCHITECTURE.md` D9）。
- `counts.dropped_rows` / `grounding_drops` 是护栏丢弃计数，非 0 就需要盯（`degraded` 会同时为 true）。
- `meta.route_trace` 是**验收硬指标**：`docs/SHOPPING_RULES.md` §4 的分支矩阵据此断言，且不会跨轮累加（D1）。
- `data.coupons` 的键是 `goods_id`；无商品上下文的通用券挂在 `"*"`（`GENERAL_KEY`）下。
- `meta.preference_summary` 用于 MaxKB 侧会话变量回传（见 `docs/MAXKB_INTEGRATION.md` §4 方案 2）。
- 降级响应示例：

```json
{"request_id": "req_...", "trace_id": "t_...",
 "answer": "暂时无法获取商品信息，请稍后再试或换个说法。",
 "data": {"intent": "other", "goods": [], "stock": {}, "coupons": {}, "out_of_stock_ids": []},
 "meta": {"mode": "rule_based", "backend": "http", "route_trace": ["supervisor", "goods_worker", "summarize"],
          "seen_goods_ids": [], "counts": {"goods": 0, "stock": 0, "coupons": 0, "dropped_rows": 0, "grounding_drops": 0},
          "degraded": true, "error_msg": "暂时无法获取商品信息，请稍后再试或换个说法。",
          "latency_ms": 412, "thread_id": "default:s_demo_5", "preference_summary": null}}
```

编程错误与数据不可达的文案是**分开**的：`Unavailable → 暂时无法获取商品信息…`，其它异常 → `系统繁忙，请稍后再试。`（`tests/test_graph_e2e.py::test_case6_backend_failure_degrades` 两个分支都钉住）。

**错误响应**

```json
// 401
{"detail": "调用凭证无效", "error_code": "UNAUTHORIZED", "request_id": "..."}
// 422
{"detail": "请求参数不合法", "error_code": "INVALID_ARGUMENT", "request_id": "...",
 "errors": [{"loc": ["body", "query"], "msg": "String should have at least 1 character"}]}
```

### 2.2 `GET /health`

```json
{"status": "ok", "version": "0.1.0", "db": "ok", "checkpointer": "ok",
 "mode": "rule_based", "backend_driver": "inprocess", "llm_enabled": false, "started": true}
```
任一子项失败 → `503` + `"status":"degraded"` + 对应字段 `"unavailable"`；checkpointer 起不来属致命项（`startup()` 直接抛 `ConfigError`，不做静默退化）。业务后端侧 `rows` 字段额外给出各表行数。

### 2.3 预留接口（M7，签名先定）

| 接口 | 用途 |
| --- | --- |
| `POST /api/shopping-agent/run/stream` | SSE；`event: node` 推进度、`event: token` 推 reason、`event: done` 推完整响应 |
| `POST /api/shopping-agent/resume` | HITL 断点恢复，入参 `{thread_id, decision}`，内部 `graph.ainvoke(Command(resume=...))` |
| `GET /api/shopping-agent/state?session_id=` | 读 checkpoint 快照（排查用，生产需管理端权限） |

---

## 三、业务后端服务 :8001

> 语义等价于 `frame.md` 里的 Java 后端；对接真实 Java 时**只需**在 `backends/http_java.py` 做路径与字段别名映射（见 `docs/EXTENSION_DB_DOCKER.md` §4）。

### 3.1 `GET /api/v1/goods/search`

| Query 参数 | 默认 | 约束 |
| --- | --- | --- |
| `q` | `""` | ≤200 字符，匹配 title/brand/tags/category |
| `category` | — | 品类码（`phone` 等），非法值返回空结果而非 422 |
| `min_price` / `max_price` | — | **单位元**（对外友好），服务端换算为 cents；`min<=max` |
| `limit` | 8 | 1–20 |
| `include_off_sale` | `false` | 仅排障用；置 `true` 才会返回 `off_sale` 商品（R-G1） |

```json
// 200
{"items": [
  {"id": "g_0001", "sku": "RM-N13P", "title": "Redmi Note 13 Pro", "category": "phone",
   "brand": "Redmi", "description": "2 亿像素主摄…", "price_cents": 169900,
   "currency": "CNY", "tags": ["5G", "拍照"], "status": "on_sale"}
 ],
 "total": 3, "limit": 8, "query": {"q": "手机", "max_price_cents": 200000, "category": "phone"}}
```
只返回 `status=on_sale`（R-G1）。`items: []` 表示无命中，不是错误。

### 3.2 `GET /api/v1/goods/{goods_id}`

`200` → 单个 `GoodsOut`；`404 GOODS_NOT_FOUND`（**不存在与下架返回同一个 404**，不泄露内部状态）。

### 3.3 `POST /api/v1/stock/query`

```json
// 请求
{"goods_ids": ["g_0001", "g_0002"]}          // 1–50 个
// 响应
{"stock": {
  "g_0001": {"goods_id": "g_0001", "on_hand": 15, "reserved": 3, "available": 12,
             "warehouses": [{"warehouse": "WH-NJ", "on_hand": 10, "reserved": 2, "available": 8},
                            {"warehouse": "WH-SH", "on_hand": 5,  "reserved": 1, "available": 4}]},
  "g_0009": {"goods_id": "g_0009", "on_hand": 0, "reserved": 0, "available": 0, "warehouses": []}
 },
 "as_of": "2026-11-11T03:21:44Z"}
```
未知 ID 也必须出现在 `stock` 里且 `available=0`（R-S2，禁止「缺键」让 Agent 猜）。

### 3.4 `POST /api/v1/coupon/query`

```json
// 请求（只接受 goods_ids；额外字段被 extra="forbid" 拒掉）
{"goods_ids": ["g_0001"]}          // 可为 []，表示纯券场景（R-C4）
// 响应
{"coupons": {"g_0001": [
   {"id": "c_0001", "code": "M1500_100", "name": "手机满1500减100", "scope": "category",
    "scope_value": ["phone"], "threshold_cents": 150000, "discount_type": "amount",
    "discount_value": 10000, "valid_from": "2026-11-01T00:00:00Z", "valid_to": "2026-12-31T15:59:59Z",
    "total_quota": -1, "remaining_quota": null, "per_user_limit": 1,
    "status": "active", "display": "满¥1500.00减¥100.00"}
 ],
 "unavailable": [
   {"code": "M500_50", "reason": "paused"},
   {"code": "M1000_200", "reason": "expired"},
   {"code": "M300_30", "reason": "quota_used_up"},
   {"code": "M2000_150", "reason": "below_threshold"}
 ]}}
```

- `unavailable[]` 只给原因码（`expired|paused|quota_used_up|below_threshold|scope_mismatch`），供排障；Agent 侧**不展示**、不进 State 白名单。
- `goods_ids: []` 时，通用券挂在键 `"*"` 下（`GENERAL_KEY`），只返回 `scope=all` 且有效、有额度的券（R-C4）。
- **没有 `user_id` 入参**：每人限用需要「用户领券记录」表，本工程尚未实现，若接收 `user_id` 就会给出无法证实的可领结论（等同编造事实）。`per_user_limit` 只做透传展示，扩展位见 `docs/SHOPPING_RULES.md` §8。
- `display` 由服务端按 `threshold_cents`/`discount_type`/`discount_value` 重新计算，wire 里的同名值不采信（`docs/ARCHITECTURE.md` D9）。

### 3.5 运维接口

| 接口 | 说明 |
| --- | --- |
| `POST /api/v1/admin/seed` `{"reset": true}` | 重新灌 `src/shopping_backend/seeds/*.json`；仅 `APP_ENV=dev|test` 可用，否则 403。返回 `{"loaded": {...}, "rows": {...}}` |
| `GET /api/v1/goods-meta/categories` | 当前库存数据里实际存在的品类码，供 demo 与 MaxKB 工具文档引用 |
| `GET /health` | `{"status":"ok","version":"0.1.0","db":"ok","engine":{...},"rows":{"goods":12,"stock":11,"coupon":9}}` |

---

## 四、Agent 内部调用业务后端的映射（driver 视图）

| `StorefrontBackend` 方法 | `inprocess` | `http` | `http_java`（示例映射，待你方确认） |
| --- | --- | --- | --- |
| `search_goods(ctx, GoodsQuery)` | `services.goods.search()` | `GET /api/v1/goods/search` | `GET /api/product/list`：`q→keyword`、`max_price_cents→maxPrice(元)`、`items→data.resultList`、`price_cents→price*100` |
| `get_stock(ctx, ids)` | `services.stock.query_available()` | `POST /api/v1/stock/query` | `POST /api/sku/stock/batch`：`goods_ids→skuIds`、`available→sellableQty` |
| `get_coupon(ctx, ids)` | `services.coupon.pick_available()` | `POST /api/v1/coupon/query` | `POST /api/promotion/available`：`code→couponCode`、`threshold_cents→startAmount*100` |

约束：三个 driver 必须通过 `tests/test_backends.py` 的同一份契约断言表（含超时、5xx、脏数据、缺键四种场景）；差异只允许出现在「协议/字段映射」，不允许出现在「业务口径」——口径唯一来源是 `services/`。

---

## 五、curl 速查

```bash
# Agent（inprocess 模式，只需 8000）
curl -s -X POST http://127.0.0.1:8000/api/shopping-agent/run \
  -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{"user_id":"u_1001","session_id":"s_demo","query":"推荐 2000 元以内的手机"}'

# 缺货 → 券分支（用例 3）
curl -s -X POST http://127.0.0.1:8000/api/shopping-agent/run \
  -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{"user_id":"u_1001","session_id":"s_demo3","query":"卖断货的耳机有优惠券吗"}'

# 库存直查（用例 2，内部仍先解析商品 ID）
curl -s -X POST http://127.0.0.1:8000/api/shopping-agent/run \
  -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{"user_id":"u_1001","session_id":"s_demo2","query":"Redmi Note 13 Pro 有货吗"}'

# 业务后端直连（http / http_java driver 调试用）
curl -s "http://127.0.0.1:8001/api/v1/goods/search?category=phone&max_price=2000&limit=8"
curl -s -X POST http://127.0.0.1:8001/api/v1/stock/query \
  -H "Content-Type: application/json" -d '{"goods_ids":["g_0001","g_0009"]}'
curl -s -X POST http://127.0.0.1:8001/api/v1/coupon/query \
  -H "Content-Type: application/json" -d '{"goods_ids":["g_0001"]}'
curl -s -X POST http://127.0.0.1:8001/api/v1/admin/seed -H "Content-Type: application/json" -d '{"reset":true}'
```

两条更省事的验证路径：

```bash
uv run python scripts/run_demo.py    # 自起 3 个进程（agent / business / 一个打不通后端的降级实例），跑完 7 条用例并校验 route_trace
bash scripts/demo_curl.sh            # 上面的 curl 串，需要自己先起服务（bash scripts/run_dev.sh）
```

`scripts/run_demo.py` 会按 `docs/SHOPPING_RULES.md` §9 自动校验 `route_trace` 与关键字段并以退出码反映结果；`scripts/demo_curl.sh` 只是同一批请求的手工版，不做断言。
