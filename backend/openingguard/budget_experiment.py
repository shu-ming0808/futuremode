"""Independent, synthetic, budget-capped replay; does not change the API simulator.

Truth generates arrivals. Only timestamped public events enter a selector.
Fixture selections test the mechanism, NOT LLM quality or predictive skill.
"""

from collections import deque
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Callable

import numpy as np


CONFIG_PATH = Path(__file__).parent / "data" / "experiments" / "event_budget_v2.json"
STRATEGIES = ("fixed12", "scheduled", "reactive", "event", "perfect_timing")


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class PublicEvent:
    event_id: str
    published_at: float
    available_at: float
    expected_start: float
    operation_note: str
    source: str = "synthetic_timestamped_fixture"

    def __post_init__(self) -> None:
        if not 0 <= self.published_at <= self.available_at or self.expected_start < 0:
            raise ValueError("Invalid event timestamps")


@dataclass(frozen=True)
class Decision:
    event: PublicEvent
    decided_at: float
    risk_id: str | None
    severity: str
    source: str

    def __post_init__(self) -> None:
        if self.decided_at < self.event.available_at:
            raise ValueError("Decision cannot precede information availability")
        if self.severity not in {"low", "medium", "high", "uncertain", "no_match"}:
            raise ValueError("Invalid severity")


@dataclass(frozen=True)
class Truth:
    case_id: str
    peak_start: float | None
    peak_duration: float
    peak_rps: float
    downstream_rps: float | None = None


def worker_rps(c: dict) -> float:
    return c["concurrency_slots"] / c["mean_service_seconds"] * c["efficiency"]


def validate_config(c: dict) -> None:
    positive = ("horizon_seconds", "dt_seconds", "base_workers", "fixed_workers",
                "burst_workers", "concurrency_slots", "mean_service_seconds", "efficiency",
                "database_rps", "gateway_rps", "queue_capacity", "slo_seconds",
                "reactive_poll_seconds", "budget_worker_minutes", "controller_poll_seconds",
                "scale_down_min_high_seconds", "scale_down_observation_seconds",
                "scale_down_confirmation_seconds", "warm_pool_workers",
                "warm_pool_hold_seconds")
    if any(not math.isfinite(c[k]) or c[k] <= 0 for k in positive):
        raise ValueError("Positive finite parameters required")
    if not 0 < c["efficiency"] <= 1 or c["day_sigma"] < 0 or c["warmup_seconds"] < 0:
        raise ValueError("Invalid service or noise settings")
    if not c["slo_seconds"] <= c["arrival_guard_seconds"] < c["horizon_seconds"]:
        raise ValueError("Leave at least one SLO interval without arrivals at the end")
    if not c["base_workers"] <= c["fixed_workers"] <= c["burst_workers"]:
        raise ValueError("Invalid worker bounds")
    if not c["base_workers"] <= c["warm_pool_workers"] <= c["burst_workers"]:
        raise ValueError("Warm pool must stay inside worker bounds")
    if not 0 < c["scale_down_utilization_threshold"] < c["scale_up_utilization_threshold"] <= 1:
        raise ValueError("Scale thresholds require hysteresis")
    if c["scale_down_queue_threshold"] < 0:
        raise ValueError("Scale-down Queue threshold cannot be negative")
    if c["fixed_workers"] * c["horizon_seconds"] > c["budget_worker_minutes"] * 60 + 1e-8:
        raise ValueError("Fixed baseline exceeds budget")
    if any(not c["base_workers"] <= n <= c["burst_workers"]
           for n in c["severity_workers"].values()):
        raise ValueError("Severity plan outside worker limits")
    for k in ("horizon_seconds", "arrival_guard_seconds", "warmup_seconds",
              "reactive_poll_seconds", "controller_poll_seconds",
              "scale_down_min_high_seconds", "scale_down_observation_seconds",
              "scale_down_confirmation_seconds", "warm_pool_hold_seconds"):
        ticks = c[k] / c["dt_seconds"]
        if not math.isclose(ticks, round(ticks)):
            raise ValueError(f"{k} must align with dt")


def generate_truth(c: dict, truth: Truth, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """No Decision or PublicEvent argument: predictions cannot change test traffic."""
    validate_config(c)
    if truth.peak_duration < 0 or truth.peak_rps < 0:
        raise ValueError("Negative workload")
    if truth.downstream_rps is not None and truth.downstream_rps <= 0:
        raise ValueError("Invalid downstream cap")
    rng = np.random.default_rng(seed)
    time = np.arange(round(c["horizon_seconds"] / c["dt_seconds"])) * c["dt_seconds"]
    peak = np.zeros(len(time), dtype=bool)
    if truth.peak_start is not None:
        peak = (time >= truth.peak_start) & (time < truth.peak_start + truth.peak_duration)
    rates = np.where(peak, truth.peak_rps, c["baseline_rps"]).astype(float)
    rates *= rng.lognormal(-0.5 * c["day_sigma"] ** 2, c["day_sigma"])
    rates[time >= c["horizon_seconds"] - c["arrival_guard_seconds"]] = 0
    return rng.poisson(rates * c["dt_seconds"]), peak


def fixture_decision(c: dict, event: PublicEvent, severity: str = "high",
                     risk_id: str = "market_volatility_order_spike") -> Decision:
    """Deterministic selection solely for replaying a hypothetical policy."""
    return Decision(event, event.available_at + c["fixture_judgment_seconds"],
                    risk_id, severity, "fixture_NOT_LLM")


def openai_decision(c: dict, event: PublicEvent) -> Decision:
    """Optional paid Agent path. Capacity remains selected by versioned configuration."""
    from .agent import select_public_event

    started = time.perf_counter()
    selection = select_public_event(event.operation_note, asdict(event))
    measured_judgment_seconds = time.perf_counter() - started
    matches = selection.risk_matches
    chosen = matches[0] if matches else None
    return Decision(
        event=event,
        decided_at=event.available_at + measured_judgment_seconds,
        risk_id=chosen.risk_id if chosen else None,
        severity=chosen.severity if chosen else "no_match",
        source="openai_three_judges_public_event",
    )


def make_cases(c: dict) -> list[tuple[Truth, Decision | None]]:
    opening = c["scheduled_peak_seconds"]
    note = "公開事件可能引發明日開盤集中送單；營運團隊預警現有 worker 的處理能力可能不足。"
    event = PublicEvent("early", opening - 320, opening - 300, opening, note)
    early = fixture_decision(c, event)
    base = Truth("early_signal", opening, c["peak_duration_seconds"], c["peak_rps"])
    late = replace(event, event_id="late", published_at=opening - 20, available_at=opening - 10)
    shifted = replace(event, event_id="off_schedule", expected_start=opening + 600)
    return [
        (replace(base, case_id="normal", peak_start=None), None),
        (base, early),
        (replace(base, case_id="off_schedule", peak_start=opening + 600), fixture_decision(c, shifted)),
        (replace(base, case_id="false_alarm", peak_start=None), early),
        (replace(base, case_id="missed_signal"), None),
        (replace(base, case_id="late_signal"), fixture_decision(c, late)),
        (replace(base, case_id="long_peak", peak_duration=1500), early),
        (replace(base, case_id="downstream_limit", downstream_rps=1200), early),
    ]


def plan_event(c: dict, decision: Decision | None) -> tuple[float | None, int, str]:
    """Numerical policy is trusted configuration, never model-produced capacity."""
    base = c["base_workers"]
    if decision is None:
        return None, base, "no_event"
    if decision.severity == "uncertain":
        return None, base, "uncertain_high_preview_only"
    if decision.risk_id not in c["scalable_risk_ids"]:
        return None, base, "non_scalable_or_unmatched_risk_review"
    target = c["severity_workers"].get(decision.severity, base)
    start = max(decision.decided_at, decision.event.expected_start - c["warmup_seconds"])
    return start, target, "configured_pulse_not_capacity_optimizer"


def simulate(c: dict, truth: Truth, arrivals: np.ndarray, peak: np.ndarray,
             strategy: str, decision: Decision | None = None, *, keep_series: bool = False) -> dict:
    """FIFO fluid-throughput approximation with a minimum service-latency gate.

    All originals enter denominator, including drops and unfinished requests.
    The last SLO interval has no arrivals; no unbilled drain after the horizon.
    Burst strategies use a small hysteresis controller: minimum high-capacity hold,
    rolling low-load confirmation, a warm-pool stage, and reactive rebound. No
    retries or real worker creation are involved.
    """
    validate_config(c)
    if strategy not in STRATEGIES:
        raise ValueError("Unknown strategy")
    dt, horizon = c["dt_seconds"], c["horizon_seconds"]
    steps = round(horizon / dt)
    if len(arrivals) != steps or len(peak) != steps or np.any(arrivals < 0):
        raise ValueError("Invalid trace")
    if not np.all(arrivals == np.floor(arrivals)):
        raise ValueError("Arrival counts must be integral")
    if np.any(arrivals[round((horizon - c["arrival_guard_seconds"]) / dt):]):
        raise ValueError("Arrival guard interval must be empty")
    base, target = c["base_workers"], c["burst_workers"]
    start, reason = None, "baseline"
    if strategy == "scheduled":
        start = max(0, c["scheduled_peak_seconds"] - c["warmup_seconds"])
    elif strategy == "perfect_timing" and truth.peak_start is not None:
        start = max(0, truth.peak_start - c["warmup_seconds"])
        reason = "privileged_timing_reference_NOT_global_optimum"
    elif strategy == "event":
        start, target, reason = plan_event(c, decision)
    spare = c["budget_worker_minutes"] * 60 - base * horizon
    planned_start_tick = math.ceil(start / dt - 1e-9) if start is not None else None
    warm_ticks = round(c["warmup_seconds"] / dt)
    service_ticks = max(1, math.ceil(c["mean_service_seconds"] / dt - 1e-9))
    poll_ticks = round(c["reactive_poll_seconds"] / dt)
    controller_poll_ticks = round(c["controller_poll_seconds"] / dt)
    observation_ticks = round(c["scale_down_observation_seconds"] / dt)
    confirmation_ticks = round(c["scale_down_confirmation_seconds"] / dt)
    min_high_ticks = round(c["scale_down_min_high_seconds"] / dt)
    warm_pool_hold_ticks = round(c["warm_pool_hold_seconds"] / dt)
    db_cap, gateway_cap = c["database_rps"], c["gateway_rps"]
    if truth.downstream_rps is not None:
        db_cap, gateway_cap = min(db_cap, truth.downstream_rps), min(gateway_cap, truth.downstream_rps)
    rps = worker_rps(c)
    queue = deque()
    queued = dropped = completed = within = max_queue = 0
    latency_counts: dict[int, int] = {}
    paid_seconds = idle_normal = idle_peak = 0.0
    warm_seconds = capacity_lost_downstream = 0.0
    downstream_headroom_warning = False
    fractional_credit = 0.0
    stage = "base"
    warming_from = base
    warm_ready_tick = None
    burst_ready_tick = None
    warm_pool_enter_tick = None
    planned_triggered = False
    activated_once = False
    first_scale_up_tick = None
    first_ready_tick = None
    first_scale_down_tick = None
    base_restored_tick = None
    rebound_scale_up_count = 0
    extra_paid_seconds = 0.0
    budget_exhausted = False
    load_window = deque()
    window_arrivals = 0.0
    window_capacity = 0.0
    observed_utilization = 0.0
    low_load_since_tick = None
    series = []
    for tick in range(steps):
        if strategy != "fixed12" and not budget_exhausted:
            if stage == "warming" and tick >= warm_ready_tick:
                stage = "burst"
                burst_ready_tick = tick
                if first_ready_tick is None:
                    first_ready_tick = tick

            is_controller_tick = tick % controller_poll_ticks == 0
            queue_pressure = queued >= c["reactive_queue_threshold"]
            utilization_pressure = (
                len(load_window) >= observation_ticks
                and observed_utilization >= c["scale_up_utilization_threshold"]
            )
            low_load_sample = (
                len(load_window) >= observation_ticks
                and observed_utilization <= c["scale_down_utilization_threshold"]
                and queued <= c["scale_down_queue_threshold"]
            )
            if stage in {"burst", "warm_pool"} and low_load_sample:
                if low_load_since_tick is None:
                    low_load_since_tick = tick
            else:
                low_load_since_tick = None
            low_load_confirmed = (
                low_load_since_tick is not None
                and tick - low_load_since_tick >= confirmation_ticks
            )

            planned_due = (
                planned_start_tick is not None
                and not planned_triggered
                and tick >= planned_start_tick
            )
            reactive_due = (
                strategy == "reactive"
                and stage == "base"
                and tick % poll_ticks == 0
                and queue_pressure
            )
            rebound_due = (
                activated_once
                and stage in {"base", "warm_pool"}
                and is_controller_tick
                and (queue_pressure or utilization_pressure)
            )
            if (planned_due or reactive_due or rebound_due) and target > base:
                if planned_due:
                    planned_triggered = True
                if stage in {"base", "warm_pool"}:
                    warming_from = base if stage == "base" else min(c["warm_pool_workers"], target)
                    stage = "warming"
                    warm_ready_tick = tick + warm_ticks
                    low_load_since_tick = None
                    if activated_once:
                        rebound_scale_up_count += 1
                    else:
                        activated_once = True
                        first_scale_up_tick = tick

            if stage == "burst" and is_controller_tick:
                held_long_enough = (
                    burst_ready_tick is not None
                    and tick - burst_ready_tick >= min_high_ticks
                )
                if held_long_enough and low_load_confirmed:
                    pool_target = min(c["warm_pool_workers"], target)
                    if pool_target < target:
                        stage = "warm_pool"
                        warm_pool_enter_tick = tick
                        if first_scale_down_tick is None:
                            first_scale_down_tick = tick
                    else:
                        stage = "base"
                        base_restored_tick = tick
            elif stage == "warm_pool" and is_controller_tick:
                pool_held_long_enough = (
                    warm_pool_enter_tick is not None
                    and tick - warm_pool_enter_tick >= warm_pool_hold_ticks
                )
                if pool_held_long_enough and low_load_confirmed:
                    stage = "base"
                    base_restored_tick = tick

        if strategy == "fixed12":
            allocated = ready = c["fixed_workers"]
            controller_state = "fixed"
        elif stage == "warming":
            allocated, ready = target, warming_from
            controller_state = "warming"
        elif stage == "burst":
            allocated = ready = target
            controller_state = "burst"
        elif stage == "warm_pool":
            allocated = ready = min(c["warm_pool_workers"], target)
            controller_state = "warm_pool"
        else:
            allocated = ready = base
            controller_state = stage

        if strategy != "fixed12":
            proposed_extra = max(0, allocated - base) * dt
            if extra_paid_seconds + proposed_extra > spare + 1e-8:
                budget_exhausted = True
                stage = controller_state = "budget_exhausted"
                allocated = ready = base
                proposed_extra = 0.0
                if base_restored_tick is None:
                    base_restored_tick = tick
            extra_paid_seconds += proposed_extra
        paid_seconds += allocated * dt
        warm_seconds += (allocated - ready) * dt
        incoming = int(arrivals[tick])
        admitted = min(incoming, c["queue_capacity"] - queued)
        dropped += incoming - admitted
        if admitted:
            queue.append([tick, admitted])
            queued += admitted
        max_queue = max(max_queue, queued)
        capacity = min(ready * rps, db_cap, gateway_cap)
        capacity_lost_downstream += (ready * rps - capacity) * dt
        fractional_credit += capacity * dt
        allowance = math.floor(fractional_credit + 1e-9)
        fractional_credit -= allowance
        served = 0
        while allowance and queue and tick - queue[0][0] + 1 >= service_ticks:
            arrived, count = queue[0]
            take = min(count, allowance)
            latency_tick = tick - arrived + 1
            completed += take
            if latency_tick * dt <= c["slo_seconds"] + 1e-9:
                within += take
            latency_counts[latency_tick] = latency_counts.get(latency_tick, 0) + take
            served += take
            queued -= take
            allowance -= take
            if count == take:
                queue.popleft()
            else:
                queue[0][1] -= take
        # Idle is unused ready throughput expressed in worker-equivalent seconds,
        # not measured CPU idle. Warmup is reported separately.
        idle = max(0.0, ready * dt - served / rps)
        if peak[tick]:
            idle_peak += idle
        else:
            idle_normal += idle
        downstream_headroom_warning |= served / dt >= 0.95 * min(db_cap, gateway_cap)
        load_window.append((incoming, capacity * dt))
        window_arrivals += incoming
        window_capacity += capacity * dt
        if len(load_window) > observation_ticks:
            old_arrivals, old_capacity = load_window.popleft()
            window_arrivals -= old_arrivals
            window_capacity -= old_capacity
        observed_utilization = window_arrivals / window_capacity if window_capacity else 0.0
        if keep_series and tick % max(1, round(1 / dt)) == 0:
            series.append({"seconds": tick * dt, "allocated_workers": allocated,
                           "ready_workers": ready, "queue": queued,
                           "capacity_rps": capacity, "arrivals_rps_100ms": incoming / dt,
                           "observed_utilization": observed_utilization,
                           "controller_state": controller_state})
    total = int(arrivals.sum())
    p95 = None
    accumulated = 0
    for latency_tick, count in sorted(latency_counts.items()):
        accumulated += count
        if accumulated >= math.ceil(completed * 0.95):
            p95 = latency_tick * dt
            break
    failure_rate = (total - within) / total if total else 0.0
    cost = paid_seconds / 60
    if cost > c["budget_worker_minutes"] + 1e-6:
        raise AssertionError("Budget gate failed")
    ready_at = first_ready_tick * dt if first_ready_tick is not None else None
    if ready_at is not None and ready_at >= horizon:
        ready_at = None
    result = {
        "case_id": truth.case_id, "strategy": strategy, "orders": total,
        "accepted_within_slo": within, "accepted_within_slo_rate": 1 - failure_rate,
        "completed": completed, "completed_late": completed - within,
        "queue_dropped": dropped, "unfinished_at_horizon": queued,
        "slo_failure_rate": failure_rate, "bad_run": failure_rate > c["max_slo_failure_rate"],
        "p95_completed_only_seconds": p95, "max_queue": max_queue,
        "worker_minutes": cost, "unused_budget_worker_minutes": c["budget_worker_minutes"] - cost,
        "normal_idle_worker_equivalent_minutes": idle_normal / 60,
        "peak_idle_worker_equivalent_minutes": idle_peak / 60,
        "warmup_worker_minutes": warm_seconds / 60,
        "capacity_lost_to_downstream_orders": capacity_lost_downstream,
        "downstream_headroom_warning": bool(downstream_headroom_warning),
        "pulse_start_seconds": first_scale_up_tick * dt if first_scale_up_tick is not None else None,
        "pulse_ready_seconds": ready_at,
        "scale_down_first_step_seconds": first_scale_down_tick * dt if first_scale_down_tick is not None else None,
        "scale_down_base_seconds": base_restored_tick * dt if base_restored_tick is not None else None,
        "rebound_scale_up_count": rebound_scale_up_count,
        "budget_exhausted_warning": budget_exhausted,
        "scale_controller": "minimum-hold+hysteresis+warm-pool-v1" if strategy != "fixed12" else "fixed",
        "ready_lead_seconds": truth.peak_start - ready_at if truth.peak_start is not None and ready_at is not None else None,
        "decision_source": decision.source if strategy == "event" and decision else "not_applicable",
        "decision_reason": reason,
        "trace_sha256": hashlib.sha256(np.asarray(arrivals, dtype="<i8").tobytes()).hexdigest(),
    }
    assert total == completed + dropped + queued
    if keep_series:
        result["series"] = series
    return result


def run_cases(c: dict, seeds: list[int], cases=None,
              decision_provider: Callable[[PublicEvent], Decision] | None = None) -> list[dict]:
    """Provider called once per unique public event, NOT once per Monte Carlo seed."""
    cases = make_cases(c) if cases is None else cases
    if not seeds or len(set(seeds)) != len(seeds):
        raise ValueError("Use nonempty, unique seeds")
    prepared = []
    cache = {}
    for truth, decision in cases:
        if decision is not None and decision_provider is not None:
            event = decision.event
            if event not in cache:
                cache[event] = decision_provider(event)
                if cache[event].event != event:
                    raise ValueError("Provider returned a decision for a different event")
            decision = cache[event]
        prepared.append((truth, decision))
    rows = []
    for truth, decision in prepared:
        for seed in seeds:
            arrivals, peak = generate_truth(c, truth, seed)
            for strategy in STRATEGIES:
                row = simulate(c, truth, arrivals, peak, strategy, decision)
                rows.append({**row, "seed": seed, "config_version": c["version"]})
    return rows


def wilson_interval(successes: int, n: int) -> tuple[float, float]:
    if n < 1 or not 0 <= successes <= n:
        raise ValueError("Invalid Bernoulli counts")
    z = 1.959963984540054
    p = successes / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0, center - half), min(1, center + half)


def paired_interval(differences, seed: int = 20260905, resamples: int = 5000) -> dict:
    """Bootstrap independent day-level paired differences; never individual orders."""
    values = np.asarray(differences, dtype=float)
    if len(values) < 2 or not np.all(np.isfinite(values)):
        raise ValueError("At least two finite paired runs required")
    rng = np.random.default_rng(seed)
    means = np.array([rng.choice(values, len(values), replace=True).mean() for _ in range(resamples)])
    low, high = np.quantile(means, [0.025, 0.975])
    return {"mean": float(values.mean()), "ci95_low": float(low), "ci95_high": float(high), "n": len(values)}
