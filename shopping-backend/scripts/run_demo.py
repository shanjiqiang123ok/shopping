"""End-to-end demo: start the real services as separate processes and run the 7 verification
cases of docs/SHOPPING_RULES.md §9 against them over HTTP.

No Docker, no MySQL, no API key. Exit code is non-zero if any case fails, so CI can gate on it.

    uv run python scripts/run_demo.py
    uv run python scripts/run_demo.py --keep-alive   # leave the services running afterwards
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shopping_backend.core.config import Settings  # noqa: E402
from shopping_backend.core.errors import Unavailable  # noqa: E402

AGENT_PORT = 8000
BUSINESS_PORT = 8001
DEGRADED_PORT = 8002


@dataclass(slots=True)
class Case:
    name: str
    verify: Any  # callable(httpx.Client, str) -> tuple[bool, str]
    base: str = "agent"


@dataclass(slots=True)
class Result:
    name: str
    ok: bool
    detail: str


def _payload(query: str, session_id: str, **extra: Any) -> dict[str, Any]:
    return {"user_id": "u_demo", "session_id": session_id, "query": query, **extra}


def _trace(body: dict[str, Any]) -> list[str]:
    return list(body.get("meta", {}).get("route_trace", []))


# ------------------------------------------------------------------ the seven cases


def case1(client: httpx.Client, base: str) -> tuple[bool, str]:
    body = client.post(f"{base}/api/shopping-agent/run", json=_payload("推荐 2000 元以内的手机", "s_demo_1")).json()
    ids = sorted(item["id"] for item in body["data"]["goods"])
    ok = ids == ["g_0001", "g_0002", "g_0003"] and "coupon_worker" not in _trace(body) and "¥1699.00" in body["answer"]
    return ok, f"ids={ids} trace={_trace(body)}"


def case2(client: httpx.Client, base: str) -> tuple[bool, str]:
    body = client.post(f"{base}/api/shopping-agent/run", json=_payload("Redmi Note 13 Pro 有货吗", "s_demo_2")).json()
    goods = body["data"]["goods"]
    ok = bool(goods) and goods[0]["id"] == "g_0001" and goods[0]["available"] == 12 and "现货12件" in body["answer"]
    return ok, f"resolved_id={goods[0]['id'] if goods else None} available={goods[0]['available'] if goods else None}"


def case3(client: httpx.Client, base: str) -> tuple[bool, str]:
    body = client.post(
        f"{base}/api/shopping-agent/run", json=_payload("这款卖断了的耳机有优惠券吗", "s_demo_3")
    ).json()
    ok = "coupon_worker" in _trace(body) and "M199_20" in body["answer"] and body["data"]["intent"] == "composite"
    return ok, f"intent={body['data']['intent']} trace={_trace(body)}"


def case4(client: httpx.Client, base: str) -> tuple[bool, str]:
    body = client.post(f"{base}/api/shopping-agent/run", json=_payload("有什么满减活动", "s_demo_4")).json()
    ok = _trace(body) == ["supervisor", "coupon_worker", "summarize"] and "M5000_600" in body["answer"]
    return ok, f"trace={_trace(body)}"


def case5(client: httpx.Client, base: str) -> tuple[bool, str]:
    body = client.post(f"{base}/api/shopping-agent/run", json=_payload("推荐 2000 元以内的手机", "s_demo_5")).json()
    ok = (
        body["meta"]["degraded"] is True
        and body["answer"] == Unavailable.friendly
        and "Traceback" not in json.dumps(body)
    )
    return ok, f"degraded={body['meta']['degraded']} answer={body['answer'][:24]}"


def case6(client: httpx.Client, base: str) -> tuple[bool, str]:
    response = client.post(
        f"{base}/api/shopping-agent/run", json=_payload("推荐手机", "s_demo_6"), headers={"Authorization": "***"}
    )
    ok = response.status_code == 401 and response.json().get("error_code") == "UNAUTHORIZED"
    return ok, f"status={response.status_code}"


def case7(client: httpx.Client, base: str) -> tuple[bool, str]:
    first = client.post(f"{base}/api/shopping-agent/run", json=_payload("推荐 2000 元以内的手机", "s_demo_7")).json()
    second = client.post(f"{base}/api/shopping-agent/run", json=_payload("第二款有货吗", "s_demo_7")).json()
    ordered = sorted(first["data"]["goods"], key=lambda item: item["price_cents"])
    expected = ordered[1]["id"] if len(ordered) > 1 else None
    got = [item["id"] for item in second["data"]["goods"]]
    ok = got == [expected] and _trace(second) == ["supervisor", "goods_worker", "stock_worker", "summarize"]
    return ok, f"expected={[expected]} got={got} trace={_trace(second)}"


CASES = [
    Case("1 选品有货 → 跳过优惠券", case1),
    Case("2 具名商品查库存（先解析ID）", case2),
    Case("3 全缺货 → 走优惠券", case3),
    Case("4 纯券查询", case4),
    Case("5 业务后端不可达 → 200 降级文案", case5, base="degraded"),
    Case("6 错误 token → 401 不进图", case6),
    Case("7 同会话续问「第二款」（checkpoint）", case7),
]


# ------------------------------------------------------------------ process management


def _env(**overrides: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update({key: str(value) for key, value in overrides.items()})
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def start(module: str, port: int, env: dict[str, str], logfile: Path) -> subprocess.Popen:
    # Output goes to a file, never PIPE: an unread pipe buffer fills after a few KB and the
    # server blocks mid-request, which looks to the client like a hang.
    handle = logfile.open("w", encoding="utf-8", errors="replace")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            module,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    process.log_handle = handle  # type: ignore[attr-defined]
    process.log_path = logfile  # type: ignore[attr-defined]
    return process


def wait_health(client: httpx.Client, base: str, process: subprocess.Popen, timeout: float = 40.0) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            tail = Path(process.log_path).read_text(encoding="utf-8", errors="replace")[-2000:]
            msg = f"service exited early on {base}\n{tail}"
            raise RuntimeError(msg)
        try:
            response = client.get(f"{base}/health", timeout=2.0)
            if response.status_code == 200:
                return
            last = response.text[:200]
        except Exception as exc:
            last = str(exc)[:200]
        time.sleep(0.4)
    msg = f"service never became healthy on {base}: {last} (see {process.log_path})"
    raise RuntimeError(msg)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Run the seven end-to-end checks against live services.")
    parser.add_argument("--keep-alive", action="store_true", help="leave the services running after the checks")
    args = parser.parse_args(argv)

    settings = Settings()
    token = settings.AGENT_INTERNAL_TOKEN or "dev-token"
    agent_base = f"http://127.0.0.1:{AGENT_PORT}"
    business_base = f"http://127.0.0.1:{BUSINESS_PORT}"
    degraded_base = f"http://127.0.0.1:{DEGRADED_PORT}"

    common = {
        "APP_ENV": "dev",
        "AGENT_MODE": "rule_based",
        "AGENT_INTERNAL_TOKEN": token,
        "SEED_ON_STARTUP": "true",
        "CHECKPOINT_DB_PATH": "./data/checkpoints_demo.db",
    }
    logs_dir = ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    processes: list[subprocess.Popen] = [
        start(
            "shopping_backend.business_api.main:app",
            BUSINESS_PORT,
            _env(**common, DATABASE_URL=settings.DATABASE_URL),
            logs_dir / "demo-business.log",
        ),
        start(
            "shopping_backend.agent_api.main:app",
            AGENT_PORT,
            _env(**common, BACKEND_DRIVER="inprocess"),
            logs_dir / "demo-agent.log",
        ),
        start(
            "shopping_backend.agent_api.main:app",
            DEGRADED_PORT,
            _env(
                **common,
                BACKEND_DRIVER="http",
                BUSINESS_API_BASE_URL="http://127.0.0.1:9",
                BUSINESS_API_TIMEOUT_S="0.4",
                BUSINESS_API_MAX_RETRY="0",
            ),
            logs_dir / "demo-agent-degraded.log",
        ),
    ]

    auth = {"Authorization": f"Bearer {token}"}
    results: list[Result] = []
    try:
        with httpx.Client(headers=auth, timeout=20.0) as client:
            wait_health(client, agent_base, processes[1])
            wait_health(client, business_base, processes[0])
            wait_health(client, degraded_base, processes[2])

            print(f"services up: agent={agent_base} business={business_base} degraded={degraded_base}\n")
            bases = {"agent": agent_base, "business": business_base, "degraded": degraded_base}
            for case in CASES:
                try:
                    ok, detail = case.verify(client, bases[case.base])
                except Exception as exc:
                    ok, detail = False, f"{type(exc).__name__}: {exc}"
                results.append(Result(case.name, ok, detail))
                print(f"[{'PASS' if ok else 'FAIL'}] {case.name}\n       {detail}")

            print("\n--- 一次完整回答（用例 1） ---")
            sample = client.post(
                f"{agent_base}/api/shopping-agent/run", json=_payload("推荐 2000 元以内的手机，有券吗", "s_demo_show")
            ).json()
            print(sample["answer"])
            print(
                f"\nmeta: intent={sample['data']['intent']} trace={sample['meta']['route_trace']} degraded={sample['meta']['degraded']}"
            )
    finally:
        if args.keep_alive:
            print("\n--keep-alive: services left running; stop them with Ctrl+C in their own windows")
        else:
            for process in processes:
                process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    process.kill()
            for process in processes:
                handle = getattr(process, "log_handle", None)
                if handle is not None:
                    handle.close()

    failed = [result for result in results if not result.ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} 用例通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
