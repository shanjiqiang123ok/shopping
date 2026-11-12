#!/usr/bin/env bash
# Hand-runnable curl set (docs/API.md §5). Start the services first:
#   BACKEND_DRIVER=http ./scripts/run_dev.sh
# Use this instead of scripts/run_demo.py when you want to watch requests yourself.
set -uo pipefail

AGENT="${AGENT:-http://127.0.0.1:8000}"
BUSINESS="${BUSINESS:-http://127.0.0.1:8001}"
TOKEN="${TOKEN:-dev-token}"
H_AUTH=(-H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json")

say() { printf '\n===== %s\n' "$1"; }

say "health"
curl -s "${AGENT}/health" | python -m json.tool

say "用例1 选品有货（应跳过优惠券）"
curl -s -X POST "${AGENT}/api/shopping-agent/run" "${H_AUTH[@]}" \
  -d '{"user_id":"u_1001","session_id":"s_curl_1","query":"推荐 2000 元以内的手机"}' | python -m json.tool

say "用例2 具名商品查库存（内部先解析ID）"
curl -s -X POST "${AGENT}/api/shopping-agent/run" "${H_AUTH[@]}" \
  -d '{"user_id":"u_1001","session_id":"s_curl_2","query":"Redmi Note 13 Pro 有货吗"}' | python -m json.tool

say "用例3 全缺货 → 优惠券"
curl -s -X POST "${AGENT}/api/shopping-agent/run" "${H_AUTH[@]}" \
  -d '{"user_id":"u_1001","session_id":"s_curl_3","query":"这款卖断了的耳机有优惠券吗"}' | python -m json.tool

say "用例4 纯券查询"
curl -s -X POST "${AGENT}/api/shopping-agent/run" "${H_AUTH[@]}" \
  -d '{"user_id":"u_1001","session_id":"s_curl_4","query":"有什么满减活动"}' | python -m json.tool

say "用例6 错误 token → 401"
curl -s -o /dev/null -w '%{http_code}\n' -X POST "${AGENT}/api/shopping-agent/run" \
  -H "Authorization: Bearer wrong" -H "Content-Type: application/json" \
  -d '{"user_id":"u_1001","session_id":"s_curl_6","query":"推荐手机"}'

say "用例7 同会话续问（checkpoint 复用候选）"
curl -s -X POST "${AGENT}/api/shopping-agent/run" "${H_AUTH[@]}" \
  -d '{"user_id":"u_1001","session_id":"s_curl_7","query":"推荐 2000 元以内的手机"}' >/dev/null
curl -s -X POST "${AGENT}/api/shopping-agent/run" "${H_AUTH[@]}" \
  -d '{"user_id":"u_1001","session_id":"s_curl_7","query":"第二款有货吗"}' | python -m json.tool

say "业务后端直连（BACKEND_DRIVER=http 时的对端）"
curl -s "${BUSINESS}/api/v1/goods/search?category=phone&max_price=2000&limit=8" | python -m json.tool
curl -s -X POST "${BUSINESS}/api/v1/stock/query" -H "Content-Type: application/json" \
  -d '{"goods_ids":["g_0001","g_0008"]}' | python -m json.tool
curl -s -X POST "${BUSINESS}/api/v1/coupon/query" -H "Content-Type: application/json" \
  -d '{"goods_ids":["g_0001"]}' | python -m json.tool
