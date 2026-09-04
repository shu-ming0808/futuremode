"""Portable deterministic core for the OpeningGuard throwaway prototype.

The model is intentionally synthetic. It uses 100 ms FIFO cohorts so a large
Monte Carlo comparison stays fast enough for a hackathon demo.
"""

from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any, Iterable
import uuid

import numpy as np


PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_DIR / "data"
SCENARIO_DIR = DATA_DIR / "scenarios"
DT = 0.1
SIMULATOR_VERSION = "0.1.0-batch-prototype"
STRATEGIES = ("fixed_capacity", "reactive_autoscaling", "predictive_prewarm")


@dataclass
class Cohort:
    arrival_tick: int
    count: int
    attempt: int = 0
    timed_out: bool = False


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_scenario(scenario_id: str) -> dict[str, Any]:
    path = SCENARIO_DIR / f"{scenario_id}.json"
    if not path.exists():
        choices = ", ".join(list_scenarios())
        raise ValueError(f"未知情境 {scenario_id!r}；可用情境：{choices}")
    scenario = load_json(path)
    validate_scenario(scenario)
    return scenario


def list_scenarios() -> list[str]:
    return sorted(path.stem for path in SCENARIO_DIR.glob("*.json"))


def load_risk_catalog() -> list[dict[str, Any]]:
    return load_json(DATA_DIR / "risk_catalog.json")


def validate_scenario(s: dict[str, Any]) -> None:
    required = {"scenario_id", "duration_seconds", "traffic", "capacity", "slo", "retry"}
    missing = sorted(required - s.keys())
    if missing:
        raise ValueError(f"缺少必要欄位：{', '.join(missing)}")
    if not 0 < int(s["duration_seconds"]) <= 3600:
        raise ValueError("duration_seconds 必須介於 1～3600")
    t, c, slo, retry = s["traffic"], s["capacity"], s["slo"], s["retry"]
    positive = {
        "baseline_rps": t.get("baseline_rps"),
        "scenario_multiplier": t.get("scenario_multiplier"),
        "current_workers": c.get("current_workers"),
        "worker_capacity_rps": c.get("worker_capacity_rps"),
        "db_connections": c.get("db_connections"),
        "db_rps_per_connection": c.get("db_rps_per_connection"),
        "gateway_rps": c.get("gateway_rps"),
        "latency_seconds": slo.get("latency_seconds"),
    }
    invalid = [name for name, value in positive.items() if value is None or float(value) <= 0]
    if invalid:
        raise ValueError(f"欄位必須大於 0：{', '.join(invalid)}")
    if retry.get("policy") not in {"none", "immediate", "backoff_jitter"}:
        raise ValueError("retry.policy 必須是 none、immediate 或 backoff_jitter")
    if not 0 <= int(retry.get("max_retries", 0)) <= 3:
        raise ValueError("max_retries 必須介於 0～3")


def apply_risk_assumptions(
    scenario: dict[str, Any], risk_ids: Iterable[str], severity: str = "medium"
) -> tuple[dict[str, Any], list[str]]:
    """Apply only predefined catalog effects; never invent numeric multipliers."""
    result = deepcopy(scenario)
    catalog = {item["risk_id"]: item for item in load_risk_catalog()}
    applied: list[str] = []
    for risk_id in dict.fromkeys(risk_ids):
        item = catalog.get(risk_id)
        if not item:
            continue
        effects = item.get("simulation_assumptions", {}).get(severity, {})
        for key, value in effects.items():
            if key == "arrival_multiplier":
                result["traffic"]["scenario_multiplier"] *= float(value)
            elif key == "worker_capacity_multiplier":
                result["capacity"]["worker_capacity_rps"] *= float(value)
            elif key == "db_capacity_multiplier":
                result["capacity"]["db_rps_per_connection"] *= float(value)
            elif key == "gateway_capacity_multiplier":
                result["capacity"]["gateway_rps"] *= float(value)
            elif key == "warmup_multiplier":
                result["capacity"]["worker_warmup_seconds"] *= float(value)
            elif key == "max_retries":
                result["retry"]["max_retries"] = int(value)
        if effects:
            applied.append(risk_id)
    validate_scenario(result)
    return result, applied


def generate_arrival_trace(scenario: dict[str, Any], seed: int) -> np.ndarray:
    """Generate a decaying open spike with mixed-Poisson intensity shocks."""
    rng = np.random.default_rng(seed)
    traffic = scenario["traffic"]
    ticks = int(scenario["duration_seconds"] / DT)
    seconds = np.arange(ticks, dtype=float) * DT
    floor = 0.58
    spike = floor + (float(traffic["open_spike_ratio"]) - floor) * np.exp(
        -seconds / float(traffic["decay_seconds"])
    )
    day_sigma = float(traffic.get("intensity_sigma", 0.1))
    day_shock = rng.lognormal(mean=-0.5 * day_sigma**2, sigma=day_sigma)

    # Short correlated shocks produce bursts without claiming a fitted process.
    raw = rng.normal(0.0, float(traffic.get("burst_sigma", 0.15)), ticks)
    smooth = np.empty(ticks)
    smooth[0] = raw[0]
    for i in range(1, ticks):
        smooth[i] = 0.88 * smooth[i - 1] + math.sqrt(1 - 0.88**2) * raw[i]
    burst = np.exp(smooth - 0.5 * float(traffic.get("burst_sigma", 0.15)) ** 2)

    rate = (
        float(traffic["baseline_rps"])
        * float(traffic["scenario_multiplier"])
        * spike
        * day_shock
        * burst
    )
    return rng.poisson(np.maximum(rate * DT, 0.0)).astype(np.int64)


def _weighted_percentile(values: list[tuple[float, int]], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    total = sum(count for _, count in ordered)
    target = max(1, math.ceil(total * q))
    seen = 0
    for value, count in ordered:
        seen += count
        if seen >= target:
            return float(value)
    return float(ordered[-1][0])


def simulate_trace(
    scenario: dict[str, Any], arrivals: np.ndarray, strategy: str, seed: int
) -> dict[str, Any]:
    if strategy not in STRATEGIES:
        raise ValueError(f"未知策略：{strategy}")
    c, slo, retry = scenario["capacity"], scenario["slo"], scenario["retry"]
    rng = np.random.default_rng(seed)
    ticks = len(arrivals)
    slo_ticks = max(1, round(float(slo["latency_seconds"]) / DT))
    queue_capacity = int(c["queue_capacity"])
    current_workers = int(c["current_workers"])
    target_workers = int(c.get("target_workers", current_workers))
    if strategy == "predictive_prewarm":
        current_workers = target_workers
    scale_ready_tick: int | None = None
    queue: deque[Cohort] = deque()
    scheduled: dict[int, list[Cohort]] = defaultdict(list)

    original_requests = 0
    total_attempts = 0
    timed_out_attempts = 0
    accepted_within = 0
    accepted_after = 0
    duplicate_attempts = 0
    duplicates_prevented = 0
    failed = 0
    max_queue = 0
    db_peak = 0.0
    gateway_peak = 0.0
    worker_minutes = 0.0
    latency_weights: list[tuple[float, int]] = []

    def retry_delay_ticks(attempt: int) -> int:
        if retry["policy"] == "immediate":
            return 1
        if retry["policy"] == "backoff_jitter":
            upper = min(
                float(retry.get("max_backoff_seconds", 1.0)),
                float(retry.get("base_delay_seconds", 0.25)) * (2 ** max(0, attempt - 1)),
            )
            return max(1, round(rng.uniform(0.0, upper) / DT))
        return 0

    for tick in range(ticks):
        if scale_ready_tick is not None and tick >= scale_ready_tick:
            current_workers = target_workers
            scale_ready_tick = None

        new_count = int(arrivals[tick])
        if new_count:
            original_requests += new_count
            total_attempts += new_count
            before = new_count
            cohort = Cohort(tick, new_count)
            current = sum(item.count for item in queue)
            admitted = min(max(0, queue_capacity - current), cohort.count)
            if admitted:
                queue.append(Cohort(tick, admitted))
            failed += before - admitted

        for cohort in scheduled.pop(tick, []):
            total_attempts += cohort.count
            duplicate_attempts += cohort.count
            current = sum(item.count for item in queue)
            admitted = min(max(0, queue_capacity - current), cohort.count)
            if admitted:
                queue.append(Cohort(tick, admitted, cohort.attempt))
            failed += cohort.count - admitted

        # A client timeout does not cancel backend work. Mark it once and place
        # eligible retries at the tail later, reusing the same conceptual UUID.
        for cohort in queue:
            if not cohort.timed_out and tick - cohort.arrival_tick >= slo_ticks:
                cohort.timed_out = True
                timed_out_attempts += cohort.count
                if cohort.attempt < int(retry["max_retries"]) and retry["policy"] != "none":
                    delay = retry_delay_ticks(cohort.attempt + 1)
                    when = min(ticks - 1, tick + delay)
                    if when > tick:
                        scheduled[when].append(Cohort(when, cohort.count, cohort.attempt + 1))

        q_len = sum(item.count for item in queue)
        max_queue = max(max_queue, q_len)
        if (
            strategy == "reactive_autoscaling"
            and current_workers < target_workers
            and scale_ready_tick is None
            and q_len >= int(c["queue_threshold"])
        ):
            scale_ready_tick = tick + round(float(c["worker_warmup_seconds"]) / DT)

        worker_cap = int(current_workers * float(c["worker_capacity_rps"]) * DT)
        db_tick_cap = int(float(c["db_connections"]) * float(c["db_rps_per_connection"]) * DT)
        gateway_tick_cap = int(float(c["gateway_rps"]) * DT)
        capacity = max(0, min(worker_cap, db_tick_cap, gateway_tick_cap))
        process_count = min(q_len, capacity)
        if db_tick_cap:
            db_peak = max(db_peak, process_count / db_tick_cap)
        if gateway_tick_cap:
            gateway_peak = max(gateway_peak, process_count / gateway_tick_cap)

        remaining = process_count
        while remaining and queue:
            cohort = queue[0]
            take = min(remaining, cohort.count)
            latency = (tick - cohort.arrival_tick + 1) * DT
            if cohort.attempt == 0:
                latency_weights.append((latency, take))
                if cohort.timed_out:
                    accepted_after += take
                else:
                    accepted_within += take
            else:
                duplicates_prevented += take
            cohort.count -= take
            remaining -= take
            if cohort.count == 0:
                queue.popleft()

        worker_minutes += current_workers * DT / 60.0

    # Unfinished work is unknown at the end; count attempts already past their deadline.
    for cohort in queue:
        if not cohort.timed_out and ticks - cohort.arrival_tick >= slo_ticks:
            timed_out_attempts += cohort.count

    p95 = _weighted_percentile(latency_weights, 0.95)
    timeout_rate = timed_out_attempts / total_attempts if total_attempts else 0.0
    accepted_rate = accepted_within / original_requests if original_requests else 1.0
    retry_amplification = total_attempts / original_requests if original_requests else 1.0
    downstream_safe = float(slo["downstream_safe_utilization"])
    congested = (
        p95 >= float(slo["latency_seconds"])
        or timeout_rate >= float(slo["max_timeout_rate"])
        or max_queue >= queue_capacity
        or db_peak >= downstream_safe
        or gateway_peak >= downstream_safe
    )
    return {
        "strategy": strategy,
        "original_requests": original_requests,
        "total_attempts": total_attempts,
        "accepted_within_slo": accepted_within,
        "accepted_after_timeout": accepted_after,
        "unknown_or_failed": max(0, original_requests - accepted_within - accepted_after),
        "duplicate_attempts": duplicate_attempts,
        "duplicate_side_effect_prevented": duplicates_prevented,
        "p95_latency_ms": round(p95 * 1000, 2),
        "timeout_rate": timeout_rate,
        "accepted_within_slo_rate": accepted_rate,
        "max_queue": max_queue,
        "database_peak_utilization": db_peak,
        "gateway_peak_utilization": gateway_peak,
        "retry_amplification_factor": retry_amplification,
        "total_worker_minutes": worker_minutes,
        "congested": congested,
    }


def _wilson(successes: int, n: int, z: float = 1.96) -> list[float]:
    if n == 0:
        return [0.0, 0.0]
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denominator
    return [max(0.0, centre - half), min(1.0, centre + half)]


def summarize_runs(strategy: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    congestion_count = sum(bool(run["congested"]) for run in runs)
    n = len(runs)
    numeric_mean = (
        "timeout_rate",
        "accepted_within_slo_rate",
        "database_peak_utilization",
        "gateway_peak_utilization",
        "retry_amplification_factor",
        "total_worker_minutes",
    )
    result: dict[str, Any] = {
        "name": strategy,
        "runs": n,
        "congestion_probability": congestion_count / n,
        "congestion_probability_ci95": _wilson(congestion_count, n),
        "p95_latency_ms": float(np.median([run["p95_latency_ms"] for run in runs])),
        "max_queue": int(np.percentile([run["max_queue"] for run in runs], 95)),
    }
    for key in numeric_mean:
        result[key] = float(np.mean([run[key] for run in runs]))
    result["cost"] = result["total_worker_minutes"]
    return result


def compare_strategies(
    scenario: dict[str, Any], runs: int = 30, seed: int = 20260904
) -> list[dict[str, Any]]:
    if not 1 <= runs <= 500:
        raise ValueError("runs 必須介於 1～500")
    collected = {strategy: [] for strategy in STRATEGIES}
    for run_index in range(runs):
        trace_seed = seed + run_index * 1009
        arrivals = generate_arrival_trace(scenario, trace_seed)
        for offset, strategy in enumerate(STRATEGIES):
            collected[strategy].append(
                simulate_trace(scenario, arrivals, strategy, trace_seed + 100_000 + offset)
            )
    return [summarize_runs(strategy, collected[strategy]) for strategy in STRATEGIES]


def _passes_constraints(result: dict[str, Any], scenario: dict[str, Any]) -> bool:
    slo = scenario["slo"]
    return (
        result["p95_latency_ms"] < float(slo["latency_seconds"]) * 1000
        and result["timeout_rate"] < float(slo["max_timeout_rate"])
        and result["congestion_probability"] < float(slo["max_congestion_probability"])
        and result["database_peak_utilization"] < float(slo["downstream_safe_utilization"])
        and result["gateway_peak_utilization"] < float(slo["downstream_safe_utilization"])
    )


def search_capacity_plan(
    scenario: dict[str, Any], runs: int = 30, seed: int = 20260904
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    for workers in scenario.get("candidate_workers", [6, 8, 10, 12, 14, 16]):
        candidate = deepcopy(scenario)
        candidate["capacity"]["target_workers"] = int(workers)
        run_results = []
        for run_index in range(runs):
            trace_seed = seed + run_index * 1009
            arrivals = generate_arrival_trace(candidate, trace_seed)
            run_results.append(
                simulate_trace(candidate, arrivals, "predictive_prewarm", trace_seed + 200_000)
            )
        summary = summarize_runs("predictive_prewarm", run_results)
        summary["workers"] = int(workers)
        summary["feasible"] = _passes_constraints(summary, candidate)
        candidates.append(summary)
    feasible = [item for item in candidates if item["feasible"]]
    if not feasible:
        return None, candidates
    feasible.sort(
        key=lambda item: (
            item["total_worker_minutes"],
            item["congestion_probability"],
            item["p95_latency_ms"],
            item["workers"],
        )
    )
    return feasible[0], candidates


def build_assessment(
    scenario_id: str,
    runs: int = 30,
    seed: int = 20260904,
    risk_matches: list[dict[str, Any]] | None = None,
    apply_risks: bool = False,
) -> dict[str, Any]:
    scenario = load_scenario(scenario_id)
    matches = risk_matches or []
    selected_ids = [item["risk_id"] for item in matches]
    applied: list[str] = []
    if apply_risks and selected_ids:
        scenario, applied = apply_risk_assumptions(scenario, selected_ids, "medium")
    comparison = compare_strategies(scenario, runs=runs, seed=seed)
    recommended, candidates = search_capacity_plan(scenario, runs=runs, seed=seed)
    baseline_minutes = (
        int(scenario["capacity"]["current_workers"]) * int(scenario["duration_seconds"]) / 60.0
    )
    for item in comparison:
        item["baseline_worker_minutes"] = baseline_minutes
        item["additional_worker_minutes"] = max(0.0, item["total_worker_minutes"] - baseline_minutes)
    for item in candidates:
        item["baseline_worker_minutes"] = baseline_minutes
        item["additional_worker_minutes"] = max(0.0, item["total_worker_minutes"] - baseline_minutes)
    if recommended:
        warning = None
        recommendation = {
            "workers": recommended["workers"],
            "min_replicas": recommended["workers"],
            "prewarm_at": "08:50",
            "congestion_probability": recommended["congestion_probability"],
            "p95_latency_ms": recommended["p95_latency_ms"],
            "timeout_rate": recommended["timeout_rate"],
            "total_worker_minutes": recommended["total_worker_minutes"],
            "cost": recommended["total_worker_minutes"],
        }
    else:
        warning = "無安全的 worker-only 方案；需調整 DB／交易閘道容量、限流、降載或人工處理。"
        recommendation = None
    return {
        "run_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "simulator_version": SIMULATOR_VERSION,
        "scenario": scenario_id,
        "label": scenario.get("label", scenario_id),
        "synthetic_assumption": True,
        "random_seed": seed,
        "runs": runs,
        "risk_matches": matches,
        "applied_medium_risk_assumptions": applied,
        "scenarios": comparison,
        "recommended": recommendation,
        "candidate_plans": candidates,
        "warning": warning,
        "approval_status": "pending_human_approval",
        "limitations": [
            "合成 RPS，不代表任何券商真實容量",
            "100 ms 批次 FIFO 模型，不是正式交易系統",
            "DB 與交易閘道只是假設的容量硬上限",
        ],
    }
