"""Run a repeatable local calibration against the mock order HTTP endpoint."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
from time import perf_counter
from typing import Any
from uuid import UUID

import httpx
import numpy as np


def _order_payload(index: int, phase: str) -> dict[str, Any]:
    namespace = int.from_bytes(hashlib.sha256(phase.encode()).digest()[:8], "big")
    return {
        "order_id": str(UUID(int=(namespace << 64) + index)),
        "account_id": "DEMO-ACCOUNT",
        "symbol": "2330",
        "side": "buy",
        "quantity": 1,
    }


async def _one_request(
    client: httpx.AsyncClient, index: int, phase: str
) -> dict[str, float | bool]:
    started = perf_counter()
    try:
        response = await client.post("/api/mock-orders", json=_order_payload(index, phase))
        elapsed_ms = (perf_counter() - started) * 1000
        if response.status_code != 200:
            return {"ok": False, "latency_ms": elapsed_ms}
        payload = response.json()
        return {
            "ok": True,
            "latency_ms": elapsed_ms,
            "server_total_ms": float(payload["server_total_ms"]),
            "queue_wait_ms": float(payload["queue_wait_ms"]),
            "simulated_work_ms": float(payload["simulated_work_ms"]),
        }
    except (httpx.HTTPError, ValueError, KeyError):
        return {"ok": False, "latency_ms": (perf_counter() - started) * 1000}


def _summarize(
    samples: list[dict[str, float | bool]], elapsed_seconds: float
) -> dict[str, float | int]:
    successful = [sample for sample in samples if sample["ok"]]
    latencies = np.array([sample["latency_ms"] for sample in successful], dtype=float)
    server_times = np.array(
        [sample["server_total_ms"] for sample in successful], dtype=float
    )
    queue_waits = np.array(
        [sample["queue_wait_ms"] for sample in successful], dtype=float
    )
    simulated_work = np.array(
        [sample["simulated_work_ms"] for sample in successful], dtype=float
    )

    def metric(values: np.ndarray, fn: str) -> float:
        if not len(values):
            return math.nan
        return float(np.mean(values) if fn == "mean" else np.percentile(values, 95))

    return {
        "requests": len(samples),
        "completed": len(successful),
        "errors": len(samples) - len(successful),
        "error_rate": (len(samples) - len(successful)) / len(samples) if samples else 1.0,
        "throughput_rps": len(successful) / elapsed_seconds if elapsed_seconds else 0.0,
        "mean_end_to_end_ms": metric(latencies, "mean"),
        "p95_end_to_end_ms": metric(latencies, "p95"),
        "mean_server_ms": metric(server_times, "mean"),
        "p95_server_ms": metric(server_times, "p95"),
        "mean_simulated_work_ms": metric(simulated_work, "mean"),
        "p95_simulated_work_ms": metric(simulated_work, "p95"),
        "mean_queue_wait_ms": metric(queue_waits, "mean"),
        "p95_queue_wait_ms": metric(queue_waits, "p95"),
    }


async def _closed_loop(
    base_url: str, offered_concurrency: int, duration: float, phase: str
) -> dict[str, Any]:
    samples: list[dict[str, float | bool]] = []
    counter = 0
    lock = asyncio.Lock()
    started = perf_counter()
    deadline = started + duration

    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        async def worker(worker_index: int) -> None:
            nonlocal counter
            while perf_counter() < deadline:
                async with lock:
                    index = counter
                    counter += 1
                samples.append(await _one_request(client, index, f"{phase}-{worker_index}"))

        await asyncio.gather(*(worker(i) for i in range(offered_concurrency)))

    elapsed = perf_counter() - started
    summary = _summarize(samples, elapsed)
    mean_service_seconds = summary["mean_simulated_work_ms"] / 1000
    summary.update(
        {
            "offered_concurrency": offered_concurrency,
            "elapsed_seconds": elapsed,
            "offered_concurrency_efficiency": (
                summary["throughput_rps"] * mean_service_seconds / offered_concurrency
                if offered_concurrency
                else 0.0
            ),
        }
    )
    return summary


async def _open_loop(
    base_url: str, target_rps: int, duration: float, phase: str
) -> dict[str, Any]:
    total = max(1, round(target_rps * duration))
    started = perf_counter()
    tasks: list[asyncio.Task[dict[str, float | bool]]] = []
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        for index in range(total):
            scheduled = started + index / target_rps
            await asyncio.sleep(max(0.0, scheduled - perf_counter()))
            tasks.append(asyncio.create_task(_one_request(client, index, phase)))
        samples = await asyncio.gather(*tasks)
    elapsed = perf_counter() - started
    summary = _summarize(samples, elapsed)
    summary.update(
        {
            "target_rps": target_rps,
            "elapsed_seconds": elapsed,
            "completion_throughput_ratio": summary["throughput_rps"] / target_rps,
        }
    )
    return summary


def _start_server(port: int, server_concurrency: int) -> subprocess.Popen[str]:
    environment = os.environ.copy()
    environment["MOCK_ORDER_CONCURRENCY"] = str(server_concurrency)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        creationflags=flags,
    )


async def _wait_until_ready(base_url: str) -> None:
    async with httpx.AsyncClient(timeout=1.0) as client:
        for _ in range(50):
            try:
                if (await client.get(f"{base_url}/api/health")).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
    raise RuntimeError("Mock order calibration server did not become ready")


async def run_calibration(args: argparse.Namespace) -> dict[str, Any]:
    base_url = f"http://127.0.0.1:{args.port}"
    server = _start_server(args.port, args.server_concurrency)
    try:
        await _wait_until_ready(base_url)
        await _closed_loop(base_url, min(args.concurrency), args.warmup, "warmup")
        concurrency_results = [
            await _closed_loop(base_url, value, args.duration, f"concurrency-{value}")
            for value in args.concurrency
        ]
        rate_results = [
            await _open_loop(base_url, value, args.duration, f"rate-{value}")
            for value in args.rates
        ]
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)

    safe = [
        row
        for row in rate_results
        if row["p95_end_to_end_ms"] < args.slo_ms
        and row["error_rate"] < args.max_error_rate
        and row["completion_throughput_ratio"] >= args.min_delivery_ratio
    ]
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "calibration_scope": "local_mock_only",
        "machine": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "processor": platform.processor(),
        },
        "server": {
            "host": "127.0.0.1",
            "port": args.port,
            "server_concurrency": args.server_concurrency,
            "mock_stages": ["request_validation", "database_delay", "gateway_delay"],
        },
        "acceptance_rule": {
            "p95_end_to_end_ms_below": args.slo_ms,
            "error_rate_below": args.max_error_rate,
            "completion_throughput_ratio_at_least": args.min_delivery_ratio,
        },
        "concurrency_sweep": concurrency_results,
        "rate_sweep": rate_results,
        "max_stable_target_rps": max((row["target_rps"] for row in safe), default=None),
        "limitations": [
            "Mock database and gateway stages use asyncio delays, not real dependencies.",
            "Load generator and API run on the same computer and compete for resources.",
            "The result must not be presented as production brokerage capacity.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Calibrate the local mock order API")
    parser.add_argument("--server-concurrency", type=int, default=20)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[5, 10, 20, 40])
    parser.add_argument(
        "--rates", type=int, nargs="+", default=[20, 40, 60, 80, 100, 120, 160, 200]
    )
    parser.add_argument("--duration", type=float, default=3.0)
    parser.add_argument("--warmup", type=float, default=1.0)
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--slo-ms", type=float, default=2000.0)
    parser.add_argument("--max-error-rate", type=float, default=0.001)
    parser.add_argument("--min-delivery-ratio", type=float, default=0.90)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("calibration_results") / "latest.json",
    )
    args = parser.parse_args()
    if args.server_concurrency <= 0 or min(args.concurrency) <= 0 or min(args.rates) <= 0:
        parser.error("concurrency and rates must be greater than zero")
    if args.duration <= 0 or args.warmup <= 0:
        parser.error("duration and warmup must be greater than zero")

    result = asyncio.run(run_calibration(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Saved calibration result to {args.output.resolve()}")


if __name__ == "__main__":
    main()
