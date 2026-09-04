"""Run repeatable local calibration against the mock order HTTP endpoint."""

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


CALIBRATION_PROFILES = {
    "quick": {
        "warmup_seconds": 1.0,
        "measurement_seconds": 3.0,
        "drain_seconds": 2.0,
        "repetitions": 1,
        "bootstrap_samples": 0,
        "evidence_grade": False,
    },
    "evidence": {
        "warmup_seconds": 5.0,
        "measurement_seconds": 30.0,
        "drain_seconds": 2.0,
        "repetitions": 5,
        "bootstrap_samples": 10_000,
        "evidence_grade": True,
    },
}


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
) -> dict[str, Any]:
    started = perf_counter()
    try:
        response = await client.post("/api/mock-orders", json=_order_payload(index, phase))
        elapsed_ms = (perf_counter() - started) * 1000
        if 400 <= response.status_code < 500:
            return {
                "outcome": "client_rejected",
                "latency_ms": elapsed_ms,
                "status_code": response.status_code,
            }
        if response.status_code >= 500:
            return {
                "outcome": "server_error",
                "latency_ms": elapsed_ms,
                "status_code": response.status_code,
            }
        payload = response.json()
        return {
            "outcome": "accepted",
            "latency_ms": elapsed_ms,
            "status_code": response.status_code,
            "server_total_ms": float(payload["server_total_ms"]),
            "queue_wait_ms": float(payload["queue_wait_ms"]),
            "simulated_work_ms": float(payload["simulated_work_ms"]),
        }
    except (httpx.HTTPError, ValueError, KeyError):
        return {
            "outcome": "transport_error",
            "latency_ms": (perf_counter() - started) * 1000,
            "status_code": None,
        }


def _summarize(
    samples: list[dict[str, Any]], measurement_seconds: float, slo_ms: float
) -> dict[str, float | int | bool]:
    accepted = [sample for sample in samples if sample["outcome"] == "accepted"]
    client_rejected = [
        sample for sample in samples if sample["outcome"] == "client_rejected"
    ]
    errors = [
        sample
        for sample in samples
        if sample["outcome"] in {"server_error", "transport_error", "drain_timeout"}
    ]
    capacity_samples = len(samples) - len(client_rejected)
    latencies = np.array([sample["latency_ms"] for sample in accepted], dtype=float)
    server_times = np.array(
        [sample["server_total_ms"] for sample in accepted], dtype=float
    )
    queue_waits = np.array(
        [sample["queue_wait_ms"] for sample in accepted], dtype=float
    )
    simulated_work = np.array(
        [sample["simulated_work_ms"] for sample in accepted], dtype=float
    )
    accepted_within_slo = sum(sample["latency_ms"] < slo_ms for sample in accepted)

    def metric(values: np.ndarray, fn: str) -> float:
        if not len(values):
            return math.nan
        return float(np.mean(values) if fn == "mean" else np.percentile(values, 95))

    return {
        "requests": len(samples),
        "capacity_eligible_requests": capacity_samples,
        "completed": len(accepted),
        "accepted_within_slo": accepted_within_slo,
        "accepted_within_slo_rate": (
            accepted_within_slo / capacity_samples if capacity_samples else 0.0
        ),
        "client_rejections": len(client_rejected),
        "errors": len(errors),
        "error_rate": len(errors) / capacity_samples if capacity_samples else 1.0,
        "throughput_rps": len(accepted) / measurement_seconds,
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
    base_url: str,
    offered_concurrency: int,
    duration: float,
    phase: str,
    slo_ms: float,
) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
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
    summary = _summarize(samples, elapsed, slo_ms)
    mean_service_seconds = float(summary["mean_simulated_work_ms"]) / 1000
    summary.update(
        {
            "offered_concurrency": offered_concurrency,
            "elapsed_seconds": elapsed,
            "offered_concurrency_efficiency": (
                float(summary["throughput_rps"])
                * mean_service_seconds
                / offered_concurrency
                if offered_concurrency
                else 0.0
            ),
        }
    )
    return summary


async def _open_loop(
    base_url: str,
    target_rps: int,
    duration: float,
    drain_seconds: float,
    phase: str,
    slo_ms: float,
) -> dict[str, Any]:
    total = max(1, round(target_rps * duration))
    started = perf_counter()
    tasks: list[asyncio.Task[dict[str, Any]]] = []
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        for index in range(total):
            scheduled = started + index / target_rps
            await asyncio.sleep(max(0.0, scheduled - perf_counter()))
            tasks.append(asyncio.create_task(_one_request(client, index, phase)))
        done, pending = await asyncio.wait(tasks, timeout=drain_seconds)
        samples = [task.result() for task in done if not task.cancelled()]
        pending_count = len(pending)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        samples.extend(
            {
                "outcome": "drain_timeout",
                "latency_ms": duration * 1000 + drain_seconds * 1000,
                "status_code": None,
            }
            for _ in range(pending_count)
        )
    elapsed = perf_counter() - started
    summary = _summarize(samples, duration, slo_ms)
    summary.update(
        {
            "target_rps": target_rps,
            "measurement_seconds": duration,
            "drain_seconds": drain_seconds,
            "elapsed_seconds": elapsed,
            "pending_after_drain": pending_count,
            "queue_drained_within_seconds": pending_count == 0,
            "completion_throughput_ratio": float(summary["throughput_rps"]) / target_rps,
        }
    )
    return summary


def _bootstrap_mean_ci(
    values: list[float], samples: int, seed: int
) -> list[float] | None:
    finite = np.array([value for value in values if math.isfinite(value)], dtype=float)
    if not len(finite) or samples <= 0:
        return None
    if len(finite) == 1:
        return [float(finite[0]), float(finite[0])]
    rng = np.random.default_rng(seed)
    draws = rng.choice(finite, size=(samples, len(finite)), replace=True).mean(axis=1)
    return [float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))]


def _aggregate_repetitions(
    rows: list[dict[str, Any]], bootstrap_samples: int, seed: int
) -> dict[str, Any]:
    numeric_keys = (
        "throughput_rps",
        "accepted_within_slo_rate",
        "error_rate",
        "mean_end_to_end_ms",
        "p95_end_to_end_ms",
        "mean_server_ms",
        "p95_server_ms",
        "mean_simulated_work_ms",
        "p95_simulated_work_ms",
        "mean_queue_wait_ms",
        "p95_queue_wait_ms",
        "offered_concurrency_efficiency",
        "completion_throughput_ratio",
    )
    result: dict[str, Any] = {"repetition_count": len(rows), "repetitions": rows}
    for key in numeric_keys:
        values = [float(row[key]) for row in rows if key in row]
        if not values:
            continue
        result[key] = float(np.mean(values))
    result["bootstrap_mean_ci95"] = {
        key: interval
        for index, key in enumerate(numeric_keys)
        if (values := [float(row[key]) for row in rows if key in row])
        and (
            interval := _bootstrap_mean_ci(
                values, bootstrap_samples, seed + index * 1009
            )
        )
        is not None
    }
    result["repetition_stddev"] = {
        key: float(np.std(values, ddof=1)) if len(values) > 1 else 0.0
        for key in numeric_keys
        if (values := [float(row[key]) for row in rows if key in row])
    }
    for key in ("offered_concurrency", "target_rps", "measurement_seconds", "drain_seconds"):
        if key in rows[0]:
            result[key] = rows[0][key]
    if "queue_drained_within_seconds" in rows[0]:
        result["queue_drained_within_seconds"] = all(
            bool(row["queue_drained_within_seconds"]) for row in rows
        )
        result["max_pending_after_drain"] = max(
            int(row["pending_after_drain"]) for row in rows
        )
    return result


def _passes_rate_rule(row: dict[str, Any], args: argparse.Namespace) -> bool:
    return (
        row["p95_end_to_end_ms"] < args.slo_ms
        and row["accepted_within_slo_rate"] >= args.min_accepted_within_slo_rate
        and row["error_rate"] < args.max_error_rate
        and row["client_rejections"] == 0
        and row["queue_drained_within_seconds"]
    )


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
        await _closed_loop(
            base_url,
            min(args.concurrency),
            args.warmup,
            "warmup",
            args.slo_ms,
        )
        concurrency_results = []
        for value in args.concurrency:
            rows = [
                await _closed_loop(
                    base_url,
                    value,
                    args.duration,
                    f"concurrency-{value}-repeat-{repeat}",
                    args.slo_ms,
                )
                for repeat in range(args.repetitions)
            ]
            concurrency_results.append(
                _aggregate_repetitions(
                    rows,
                    args.bootstrap_samples,
                    args.bootstrap_seed + value,
                )
            )

        rate_results = []
        for value in args.rates:
            rows = [
                await _open_loop(
                    base_url,
                    value,
                    args.duration,
                    args.drain,
                    f"rate-{value}-repeat-{repeat}",
                    args.slo_ms,
                )
                for repeat in range(args.repetitions)
            ]
            for row in rows:
                row["passes_acceptance_rule"] = _passes_rate_rule(row, args)
            aggregate = _aggregate_repetitions(
                rows,
                args.bootstrap_samples,
                args.bootstrap_seed + 10_000 + value,
            )
            aggregate["all_repetitions_pass"] = all(
                row["passes_acceptance_rule"] for row in rows
            )
            rate_results.append(aggregate)
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)

    safe = [row for row in rate_results if row["all_repetitions_pass"]]
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "calibration_scope": "local_mock_only",
        "profile": args.profile,
        "evidence_grade": args.evidence_grade,
        "claim_status": "formal_evidence" if args.evidence_grade else "indicative_only",
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
        "test_design": {
            "warmup_seconds": args.warmup,
            "measurement_seconds": args.duration,
            "drain_seconds": args.drain,
            "repetitions": args.repetitions,
            "bootstrap_samples": args.bootstrap_samples,
            "bootstrap_seed": args.bootstrap_seed,
        },
        "acceptance_rule": {
            "accepted_within_slo_rate_at_least": args.min_accepted_within_slo_rate,
            "slo_ms": args.slo_ms,
            "p95_end_to_end_ms_below": args.slo_ms,
            "server_transport_error_rate_below": args.max_error_rate,
            "client_rejections": 0,
            "queue_drained_within_seconds": args.drain,
            "all_repetitions_must_pass": True,
        },
        "concurrency_sweep": concurrency_results,
        "rate_sweep": rate_results,
        "max_stable_target_rps": max((row["target_rps"] for row in safe), default=None),
        "limitations": [
            "Mock database and gateway stages use asyncio delays, not real dependencies.",
            "Load generator and API run on the same computer and compete for resources.",
            "Quick profile is indicative only and is not formal statistical evidence.",
            "The result must not be presented as production brokerage capacity.",
        ],
    }


def _profile_value(
    args: argparse.Namespace, argument_name: str, profile_name: str
) -> Any:
    value = getattr(args, argument_name)
    return CALIBRATION_PROFILES[args.profile][profile_name] if value is None else value


def main() -> None:
    parser = argparse.ArgumentParser(description="Calibrate the local mock order API")
    parser.add_argument("--profile", choices=CALIBRATION_PROFILES, default="quick")
    parser.add_argument("--server-concurrency", type=int, default=20)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[5, 10, 20, 40])
    parser.add_argument(
        "--rates", type=int, nargs="+", default=[20, 40, 60, 80, 100, 120, 160, 200]
    )
    parser.add_argument("--duration", type=float)
    parser.add_argument("--warmup", type=float)
    parser.add_argument("--drain", type=float)
    parser.add_argument("--repetitions", type=int)
    parser.add_argument("--bootstrap-samples", type=int)
    parser.add_argument("--bootstrap-seed", type=int, default=20260904)
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--slo-ms", type=float, default=2000.0)
    parser.add_argument("--max-error-rate", type=float, default=0.001)
    parser.add_argument("--min-accepted-within-slo-rate", type=float, default=0.999)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("calibration_results") / "latest.json",
    )
    args = parser.parse_args()
    args.duration = _profile_value(args, "duration", "measurement_seconds")
    args.warmup = _profile_value(args, "warmup", "warmup_seconds")
    args.drain = _profile_value(args, "drain", "drain_seconds")
    args.repetitions = _profile_value(args, "repetitions", "repetitions")
    args.bootstrap_samples = _profile_value(
        args, "bootstrap_samples", "bootstrap_samples"
    )
    evidence_defaults = CALIBRATION_PROFILES["evidence"]
    args.evidence_grade = bool(
        args.profile == "evidence"
        and args.warmup >= evidence_defaults["warmup_seconds"]
        and args.duration >= evidence_defaults["measurement_seconds"]
        and args.drain >= evidence_defaults["drain_seconds"]
        and args.repetitions >= evidence_defaults["repetitions"]
        and args.bootstrap_samples >= evidence_defaults["bootstrap_samples"]
    )
    if args.server_concurrency <= 0 or min(args.concurrency) <= 0 or min(args.rates) <= 0:
        parser.error("concurrency and rates must be greater than zero")
    if args.duration <= 0 or args.warmup <= 0 or args.drain <= 0:
        parser.error("duration, warmup, and drain must be greater than zero")
    if args.repetitions <= 0 or args.bootstrap_samples < 0:
        parser.error("repetitions must be positive and bootstrap samples cannot be negative")
    if not 0 < args.min_accepted_within_slo_rate <= 1:
        parser.error("min accepted-within-SLO rate must be in (0, 1]")
    if not 0 <= args.max_error_rate < 1:
        parser.error("max error rate must be in [0, 1)")

    result = asyncio.run(run_calibration(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Saved calibration result to {args.output.resolve()}")


if __name__ == "__main__":
    main()
