"""Portable deterministic core for the OpeningGuard throwaway prototype.

The model is intentionally synthetic. It uses 100 ms FIFO cohorts so a large
Monte Carlo comparison stays fast enough for a hackathon demo.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict, deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .schemas import (
    AppliedRiskAssumption,
    Assessment,
    CandidatePlan,
    CapacityRecommendation,
    ResourceUtilization,
    RiskCard,
    RiskEffects,
    RiskMatch,
    Scenario,
    SimulationRunResult,
    SLOMetrics,
    StrategySummary,
    VolumeCounts,
)

PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_DIR / "data"
SCENARIO_DIR = DATA_DIR / "scenarios"
DT = 0.1
STRATEGIES = ("fixed_capacity", "reactive_autoscaling", "predictive_prewarm")
MONTE_CARLO_PROFILES = {"demo": 500, "evidence": 2_000}
MAX_MONTE_CARLO_RUNS = MONTE_CARLO_PROFILES["evidence"]


@dataclass
class Cohort:
    arrival_tick: int
    count: int
    attempt: int = 0
    timed_out: bool = False


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_scenario(scenario_id: str) -> Scenario:
    path = SCENARIO_DIR / f"{scenario_id}.json"
    if not path.exists():
        choices = ", ".join(list_scenarios())
        raise ValueError(f"未知情境 {scenario_id!r}；可用情境：{choices}")
    return Scenario.model_validate(load_json(path))


def list_scenarios() -> list[str]:
    return sorted(path.stem for path in SCENARIO_DIR.glob("*.json"))


def load_risk_catalog() -> list[dict[str, Any]]:
    return load_json(DATA_DIR / "risk_catalog.json")


def apply_risk_assumptions(
    scenario: Scenario, risk_matches: Iterable[RiskMatch | dict[str, Any] | str]
) -> tuple[Scenario, list[AppliedRiskAssumption]]:
    """Apply only catalog effects selected by risk ID and an enumerated severity.

    An uncertain judge result is intentionally simulated with the catalog's high
    assumptions, but remains labelled uncertain in the returned audit record.
    """
    result = scenario.model_copy(deep=True)
    catalog = {item["risk_id"]: item for item in load_risk_catalog()}
    applied: list[AppliedRiskAssumption] = []
    seen: set[str] = set()
    for raw_match in risk_matches:
        if isinstance(raw_match, str):
            match = RiskMatch(risk_id=raw_match)
        elif isinstance(raw_match, RiskMatch):
            match = raw_match
        else:
            match = RiskMatch.model_validate(raw_match)
        if match.risk_id in seen:
            continue
        seen.add(match.risk_id)
        item = catalog.get(match.risk_id)
        if not item:
            continue
        simulation_severity = (
            "high" if match.severity == "uncertain" else match.severity
        )
        effects = item.get("simulation_assumptions", {}).get(simulation_severity, {})
        for key, value in effects.items():
            if key == "arrival_multiplier":
                result.traffic.scenario_multiplier *= float(value)
            elif key == "worker_capacity_multiplier":
                result.capacity.worker_efficiency *= float(value)
            elif key == "db_capacity_multiplier":
                result.capacity.db_rps_per_connection *= float(value)
            elif key == "gateway_capacity_multiplier":
                result.capacity.gateway_rps *= float(value)
            elif key == "warmup_multiplier":
                result.capacity.worker_warmup_seconds *= float(value)
            elif key == "max_retries":
                result.retry.max_retries = int(value)
        applied.append(
            AppliedRiskAssumption(
                risk_id=match.risk_id,
                agent_severity=match.severity,
                simulation_assumption=simulation_severity,
                effects=RiskEffects.model_validate(effects),
            )
        )
    result = Scenario.model_validate(result.model_dump())
    return result, applied


def effective_worker_rps(capacity: Any) -> float:
    """Derive per-worker throughput from concurrency, service time, and efficiency."""
    return (
        float(capacity.worker_concurrency)
        / float(capacity.mean_service_time_seconds)
        * float(capacity.worker_efficiency)
    )


def generate_arrival_trace(scenario: Scenario, seed: int) -> np.ndarray:
    """Generate a decaying open spike with mixed-Poisson intensity shocks."""
    rng = np.random.default_rng(seed)
    traffic = scenario.traffic
    ticks = int(scenario.duration_seconds / DT)
    seconds = np.arange(ticks, dtype=float) * DT
    floor = 0.58
    spike = floor + (traffic.open_spike_ratio - floor) * np.exp(
        -seconds / traffic.decay_seconds
    )
    day_sigma = traffic.intensity_sigma
    day_shock = rng.lognormal(mean=-0.5 * day_sigma**2, sigma=day_sigma)

    # Short correlated shocks produce bursts without claiming a fitted process.
    raw = rng.normal(0.0, traffic.burst_sigma, ticks)
    smooth = np.empty(ticks)
    smooth[0] = raw[0]
    for i in range(1, ticks):
        smooth[i] = 0.88 * smooth[i - 1] + math.sqrt(1 - 0.88**2) * raw[i]
    burst = np.exp(smooth - 0.5 * traffic.burst_sigma**2)

    rate = (
        traffic.baseline_rps * traffic.scenario_multiplier * spike * day_shock * burst
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
    scenario: Scenario, arrivals: np.ndarray, strategy: str, seed: int
) -> SimulationRunResult:
    if strategy not in STRATEGIES:
        raise ValueError(f"未知策略：{strategy}")
    c, slo, retry = scenario.capacity, scenario.slo, scenario.retry
    rng = np.random.default_rng(seed)
    ticks = len(arrivals)
    slo_ticks = max(1, round(slo.latency_seconds / DT))
    queue_capacity = int(c.queue_capacity)
    current_workers = int(c.current_workers)
    target_workers = c.effective_target_workers
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
        if retry.policy == "immediate":
            return 1
        if retry.policy == "backoff_jitter":
            upper = min(
                retry.max_backoff_seconds,
                retry.base_delay_seconds * (2 ** max(0, attempt - 1)),
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
                if cohort.attempt < retry.max_retries and retry.policy != "none":
                    delay = retry_delay_ticks(cohort.attempt + 1)
                    when = min(ticks - 1, tick + delay)
                    if when > tick:
                        scheduled[when].append(
                            Cohort(when, cohort.count, cohort.attempt + 1)
                        )

        q_len = sum(item.count for item in queue)
        max_queue = max(max_queue, q_len)
        if (
            strategy == "reactive_autoscaling"
            and current_workers < target_workers
            and scale_ready_tick is None
            and q_len >= c.queue_threshold
        ):
            scale_ready_tick = tick + round(c.worker_warmup_seconds / DT)

        worker_cap = int(current_workers * effective_worker_rps(c) * DT)
        db_tick_cap = int(c.db_connections * c.db_rps_per_connection * DT)
        gateway_tick_cap = int(c.gateway_rps * DT)
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
    downstream_safe = slo.downstream_safe_utilization
    congested = (
        p95 >= slo.latency_seconds
        or timeout_rate >= slo.max_timeout_rate
        or max_queue >= queue_capacity
        or db_peak >= downstream_safe
        or gateway_peak >= downstream_safe
    )
    return SimulationRunResult(
        strategy=strategy,
        volume=VolumeCounts(
            original_requests=original_requests,
            total_attempts=total_attempts,
            accepted_within_slo=accepted_within,
            accepted_after_timeout=accepted_after,
            unknown_or_failed=max(
                0, original_requests - accepted_within - accepted_after
            ),
            duplicate_attempts=duplicate_attempts,
            duplicate_side_effect_prevented=duplicates_prevented,
        ),
        slo_metrics=SLOMetrics(
            p95_latency_ms=round(p95 * 1000, 2),
            timeout_rate=timeout_rate,
            accepted_within_slo_rate=accepted_rate,
            max_queue=max_queue,
            congested=congested,
        ),
        utilization=ResourceUtilization(
            database_peak_utilization=db_peak,
            gateway_peak_utilization=gateway_peak,
            total_worker_minutes=worker_minutes,
        ),
    )


def _wilson(successes: int, n: int, z: float = 1.96) -> list[float]:
    if n == 0:
        return [0.0, 0.0]
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denominator
    return [max(0.0, centre - half), min(1.0, centre + half)]


def summarize_runs(strategy: str, runs: list[SimulationRunResult]) -> StrategySummary:
    congestion_count = sum(run.slo_metrics.congested for run in runs)
    n = len(runs)
    congestion_ci95 = _wilson(congestion_count, n)
    total_worker_minutes = float(
        np.mean([run.utilization.total_worker_minutes for run in runs])
    )
    return StrategySummary(
        name=strategy,
        congestion_probability=congestion_count / n,
        congestion_probability_ci95=congestion_ci95,
        p95_latency_ms=float(
            np.median([run.slo_metrics.p95_latency_ms for run in runs])
        ),
        timeout_rate=float(np.mean([run.slo_metrics.timeout_rate for run in runs])),
        database_peak_utilization=float(
            np.mean([run.utilization.database_peak_utilization for run in runs])
        ),
        gateway_peak_utilization=float(
            np.mean([run.utilization.gateway_peak_utilization for run in runs])
        ),
        cost=total_worker_minutes,
    )


def compare_strategies(
    scenario: Scenario, runs: int = 500, seed: int = 20260904
) -> list[StrategySummary]:
    if not 1 <= runs <= MAX_MONTE_CARLO_RUNS:
        raise ValueError(f"runs 必須介於 1～{MAX_MONTE_CARLO_RUNS}")
    collected: dict[str, list[SimulationRunResult]] = {
        strategy: [] for strategy in STRATEGIES
    }
    for run_index in range(runs):
        trace_seed = seed + run_index * 1009
        arrivals = generate_arrival_trace(scenario, trace_seed)
        for offset, strategy in enumerate(STRATEGIES):
            collected[strategy].append(
                simulate_trace(
                    scenario, arrivals, strategy, trace_seed + 100_000 + offset
                )
            )
    return [summarize_runs(strategy, collected[strategy]) for strategy in STRATEGIES]


def _passes_constraints(result: StrategySummary, scenario: Scenario) -> bool:
    slo = scenario.slo
    return (
        result.p95_latency_ms < slo.latency_seconds * 1000
        and result.timeout_rate < slo.max_timeout_rate
        and result.congestion_probability_ci95[1] < slo.max_congestion_probability
        and result.database_peak_utilization < slo.downstream_safe_utilization
        and result.gateway_peak_utilization < slo.downstream_safe_utilization
    )


def search_capacity_plan(
    scenario: Scenario, runs: int = 500, seed: int = 20260904
) -> tuple[CandidatePlan | None, list[CandidatePlan]]:
    candidates: list[CandidatePlan] = []
    for workers in scenario.candidate_workers:
        candidate = scenario.model_copy(deep=True)
        candidate.capacity.target_workers = int(workers)
        run_results = []
        for run_index in range(runs):
            trace_seed = seed + run_index * 1009
            arrivals = generate_arrival_trace(candidate, trace_seed)
            run_results.append(
                simulate_trace(
                    candidate, arrivals, "predictive_prewarm", trace_seed + 200_000
                )
            )
        summary = summarize_runs("predictive_prewarm", run_results)
        candidates.append(
            CandidatePlan(
                **summary.model_dump(),
                workers=int(workers),
                feasible=_passes_constraints(summary, candidate),
            )
        )
    feasible = [item for item in candidates if item.feasible]
    if not feasible:
        return None, candidates
    feasible.sort(
        key=lambda item: (
            item.cost,
            item.congestion_probability,
            item.p95_latency_ms,
            item.workers,
        )
    )
    return feasible[0], candidates


def _build_risk_cards(
    matches: list[RiskMatch], applied: list[AppliedRiskAssumption]
) -> list[RiskCard]:
    """Join judge output + applied simulation effects + catalog metadata into one display card."""
    catalog = {item["risk_id"]: item for item in load_risk_catalog()}
    effects_by_id = {item.risk_id: item.effects for item in applied}
    cards = []
    for match in matches:
        item = catalog.get(match.risk_id)
        if not item:
            continue
        cards.append(
            RiskCard(
                risk_id=match.risk_id,
                title=item["title"],
                severity=match.severity,
                matched_input_text=match.matched_input_text,
                source_url=item["source_url"],
                effects=effects_by_id.get(match.risk_id, RiskEffects()),
            )
        )
    return cards


def build_assessment(
    scenario_id: str,
    runs: int = 500,
    seed: int = 20260904,
    risk_matches: Sequence[RiskMatch] | None = None,
    apply_risks: bool = False,
    run_profile: str = "custom",
) -> Assessment:
    scenario = load_scenario(scenario_id)
    matches = [
        m if isinstance(m, RiskMatch) else RiskMatch.model_validate(m)
        for m in (risk_matches or [])
    ]
    applied: list[AppliedRiskAssumption] = []
    if apply_risks and matches:
        scenario, applied = apply_risk_assumptions(scenario, matches)
    has_uncertain_risk = any(match.severity == "uncertain" for match in matches)
    comparison = compare_strategies(scenario, runs=runs, seed=seed)
    recommended, _candidates = search_capacity_plan(scenario, runs=runs, seed=seed)
    if recommended:
        warning_code = None
        recommendation = CapacityRecommendation(
            workers=recommended.workers,
            congestion_probability=recommended.congestion_probability,
            congestion_probability_ci95=recommended.congestion_probability_ci95,
            p95_latency_ms=recommended.p95_latency_ms,
            timeout_rate=recommended.timeout_rate,
            database_peak_utilization=recommended.database_peak_utilization,
            gateway_peak_utilization=recommended.gateway_peak_utilization,
            cost=recommended.cost,
        )
    else:
        warning_code = "no_feasible_plan"
        recommendation = None
    return Assessment(
        label=scenario.label_or_id,
        scenarios=comparison,
        recommended=recommendation,
        warning_code=warning_code,
        risks=_build_risk_cards(matches, applied),
        requires_human_review=has_uncertain_risk,
    )
