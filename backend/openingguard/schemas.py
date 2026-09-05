"""Pydantic models for scenario config, simulation results, and the API surface."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class TrafficConfig(BaseModel):
    baseline_rps: float = Field(gt=0)
    scenario_multiplier: float = Field(gt=0)
    open_spike_ratio: float
    decay_seconds: float
    intensity_sigma: float = 0.1
    burst_sigma: float = 0.15


class CapacityConfig(BaseModel):
    current_workers: int = Field(gt=0)
    target_workers: int | None = None
    worker_concurrency: float = Field(gt=0)
    mean_service_time_seconds: float = Field(gt=0)
    worker_efficiency: float = Field(gt=0, le=1)
    worker_warmup_seconds: float
    queue_threshold: int
    queue_capacity: int
    db_connections: float = Field(gt=0)
    db_rps_per_connection: float = Field(gt=0)
    gateway_rps: float = Field(gt=0)

    @model_validator(mode="after")
    def _default_target_workers(self) -> CapacityConfig:
        if self.target_workers is None:
            self.target_workers = self.current_workers
        return self

    @property
    def effective_target_workers(self) -> int:
        assert self.target_workers is not None
        return self.target_workers


class SLOConfig(BaseModel):
    latency_seconds: float = Field(gt=0)
    max_timeout_rate: float
    max_congestion_probability: float
    downstream_safe_utilization: float


class RetryConfig(BaseModel):
    policy: Literal["none", "immediate", "backoff_jitter"]
    max_retries: int = Field(ge=0, le=3, default=0)
    base_delay_seconds: float = 0.25
    max_backoff_seconds: float = 1.0


class Scenario(BaseModel):
    scenario_id: str
    scenario_version: str
    label: str = ""
    duration_seconds: int = Field(gt=0, le=3600)
    operation_note: str = ""
    traffic: TrafficConfig
    capacity: CapacityConfig
    slo: SLOConfig
    retry: RetryConfig
    candidate_workers: list[int] = Field(default_factory=lambda: [6, 8, 10, 12, 14, 16])
    synthetic_assumption: bool = True

    @property
    def label_or_id(self) -> str:
        return self.label or self.scenario_id


class RiskMatch(BaseModel):
    risk_id: str
    severity: Literal["low", "medium", "high", "uncertain"] = "medium"
    matched_input_text: str = ""


class RiskEffects(BaseModel):
    arrival_multiplier: float | None = None
    worker_capacity_multiplier: float | None = None
    db_capacity_multiplier: float | None = None
    gateway_capacity_multiplier: float | None = None
    warmup_multiplier: float | None = None
    max_retries: int | None = None


class RiskCard(BaseModel):
    """Agent-selected risk merged with catalog metadata and simulation effects."""

    risk_id: str
    title: str
    severity: Literal["low", "medium", "high", "uncertain"]
    matched_input_text: str
    source_url: str
    effects: RiskEffects


class SubmitRiskJudgmentMatch(BaseModel):
    """One risk in the strict Agent tool call; risk_id is narrowed per request."""

    risk_id: str
    is_at_least_medium: Literal[0, 1]
    is_high: Literal[0, 1]
    matched_input_text: str
    reason: str

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _reject_high_without_medium(self) -> SubmitRiskJudgmentMatch:
        if self.is_high and not self.is_at_least_medium:
            raise ValueError("is_high=1 時，is_at_least_medium 必須為 1")
        return self

    @property
    def severity(self) -> Literal["low", "medium", "high"]:
        if self.is_high:
            return "high"
        if self.is_at_least_medium:
            return "medium"
        return "low"


class SubmitRiskJudgment(BaseModel):
    risk_matches: list[SubmitRiskJudgmentMatch] = Field(max_length=3)
    decision_status: Literal["confirmed", "uncertain", "no_match"]
    no_confident_match: bool

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _derive_no_confident_match(self) -> SubmitRiskJudgment:
        if self.decision_status == "no_match" and self.risk_matches:
            raise ValueError("decision_status=no_match 時 risk_matches 必須為空")
        if self.decision_status != "no_match" and not self.risk_matches:
            raise ValueError(
                "decision_status=confirmed 或 uncertain 時必須提供候選 risk_matches"
            )
        # Field stays in the strict tool schema, but is derived rather than trusted.
        self.no_confident_match = self.decision_status != "confirmed"
        return self


class AgentRiskMatch(BaseModel):
    """One validated risk selected by the single OpenAI judgment."""

    risk_id: str
    severity: Literal["low", "medium", "high", "uncertain"]
    simulation_assumption: Literal["low", "medium", "high"]
    matched_input_text: str
    reason: str


class AgentDecision(BaseModel):
    """Auditable decision built from one strict OpenAI tool call."""

    risk_matches: list[AgentRiskMatch]
    no_confident_match: bool
    decision_status: Literal["confirmed", "uncertain", "no_match"]
    requires_human_review: bool
    raw_judgment: SubmitRiskJudgment
    model: str = ""

    @property
    def selected_risks(self) -> list[RiskMatch]:
        return [
            RiskMatch(
                risk_id=item.risk_id,
                severity=item.severity,
                matched_input_text=item.matched_input_text,
            )
            for item in self.risk_matches
        ]


class AppliedRiskAssumption(BaseModel):
    risk_id: str
    agent_severity: str
    simulation_assumption: str
    effects: RiskEffects


class VolumeCounts(BaseModel):
    original_requests: int
    total_attempts: int
    accepted_within_slo: int
    accepted_after_timeout: int
    unknown_or_failed: int
    duplicate_attempts: int
    duplicate_side_effect_prevented: int


class SLOMetrics(BaseModel):
    p95_latency_ms: float
    timeout_rate: float
    accepted_within_slo_rate: float
    max_queue: int
    congested: bool


class ResourceUtilization(BaseModel):
    database_peak_utilization: float
    gateway_peak_utilization: float
    total_worker_minutes: float


class SimulationRunResult(BaseModel):
    strategy: str
    volume: VolumeCounts
    slo_metrics: SLOMetrics
    utilization: ResourceUtilization


class StrategySummary(BaseModel):
    """Cross-run Monte Carlo aggregates for one capacity strategy; drives the strategy comparison chart."""

    name: str
    congestion_probability: float
    congestion_probability_ci95: list[float]
    p95_latency_ms: float
    timeout_rate: float
    database_peak_utilization: float
    gateway_peak_utilization: float
    cost: float


class CandidatePlan(StrategySummary):
    """Internal only: one candidate worker count evaluated while searching for `recommended`."""

    workers: int
    feasible: bool


class CapacityRecommendation(StrategySummary):
    workers: int


class Assessment(BaseModel):
    label: str
    scenarios: list[StrategySummary]
    recommended: CapacityRecommendation | None
    warning_code: Literal["no_feasible_plan"] | None
    risks: list[RiskCard]
    requires_human_review: bool


class SimulationRequest(BaseModel):
    scenario: str = "normal"
    profile: Literal["demo", "evidence"] = "demo"
    operation_note: str | None = None
    use_agent: bool = False


class MockOrderRequest(BaseModel):
    order_id: UUID | None = None
