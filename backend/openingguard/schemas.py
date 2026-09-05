"""Pydantic models for scenario config, simulation results, and the API surface."""

from __future__ import annotations

from typing import Any, Literal
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


class SubmitRiskJudgmentMatch(BaseModel):
    """Mirrors the judge tool-call schema; risk_id enum is narrowed dynamically per call."""

    risk_id: str
    is_at_least_medium: Literal[0, 1]
    is_high: Literal[0, 1]
    matched_input_text: str
    reason: str

    model_config = {"extra": "forbid"}


class SubmitRiskJudgment(BaseModel):
    risk_matches: list[SubmitRiskJudgmentMatch] = Field(max_length=3)
    no_confident_match: bool

    model_config = {"extra": "forbid"}


class JudgeRiskMatch(BaseModel):
    risk_id: str
    is_at_least_medium: int
    is_high: int
    matched_input_text: str
    reason: str
    severity: Literal["low", "medium", "high"]
    judge_id: str


class JudgeVote(BaseModel):
    judge_id: str
    risk_matches: list[JudgeRiskMatch]
    no_confident_match: bool


class OfflineFallbackAgent(BaseModel):
    mode: Literal["offline_fallback"]
    is_llm_result: Literal[False] = False
    decision_status: Literal["confirmed", "uncertain", "no_match"]
    reason: str
    prompt_version: str
    knowledge_base_version: str
    models_requested: list[str] | None = None


class OfflineFallbackAfterErrorAgent(BaseModel):
    mode: Literal["offline_fallback_after_api_error"]
    is_llm_result: Literal[False] = False
    decision_status: Literal["confirmed", "uncertain", "no_match"]
    reason: str
    prompt_version: str
    knowledge_base_version: str
    models_requested: list[str] | None = None


class LLMJudgeAgent(BaseModel):
    mode: Literal["openai_multi_judge_tool_calling"]
    is_llm_result: Literal[True] = True
    committee_mode: Literal["same_model_multi_judge", "multi_model_openai_judges"]
    models: list[str]
    judge_count: int
    decision_status: Literal["confirmed", "uncertain", "no_match"]
    requires_human_review: bool
    auto_approved: bool
    prompt_version: str
    knowledge_base_version: str
    tool_called: str
    judge_outputs: list[JudgeVote]
    final_explanation: str
    response_ids: list[str]


class RiskEffects(BaseModel):
    arrival_multiplier: float | None = None
    worker_capacity_multiplier: float | None = None
    db_capacity_multiplier: float | None = None
    gateway_capacity_multiplier: float | None = None
    warmup_multiplier: float | None = None
    max_retries: int | None = None


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
    name: str
    runs: int
    congestion_probability: float
    congestion_probability_ci95: list[float]
    congestion_probability_ci95_upper: float
    p95_latency_ms: float
    max_queue: int
    timeout_rate: float
    accepted_within_slo_rate: float
    database_peak_utilization: float
    gateway_peak_utilization: float
    retry_amplification_factor: float
    total_worker_minutes: float
    cost: float
    additional_worker_minutes: float = 0.0


class CandidatePlan(StrategySummary):
    workers: int
    feasible: bool


class CapacityRecommendation(BaseModel):
    workers: int
    min_replicas: int
    prewarm_at: str
    congestion_probability: float
    congestion_probability_ci95: list[float]
    p95_latency_ms: float
    timeout_rate: float
    total_worker_minutes: float
    cost: float


class DerivedParameters(BaseModel):
    effective_worker_rps_per_worker: float


class Assessment(BaseModel):
    run_id: str
    created_at: str
    scenario: str
    scenario_version: str
    label: str
    derived_parameters: DerivedParameters
    random_seed: int
    runs: int
    risk_matches: list[RiskMatch]
    applied_risk_assumptions: list[AppliedRiskAssumption]
    scenarios: list[StrategySummary]
    recommended: CapacityRecommendation | None
    candidate_plans: list[CandidatePlan]
    baseline_worker_minutes: float
    warning: str | None
    approval_status: str
    uncertain_risk_preview: bool
    agent: (
        OfflineFallbackAgent | OfflineFallbackAfterErrorAgent | LLMJudgeAgent | None
    ) = Field(default=None, discriminator="mode")


class SimulationRequest(BaseModel):
    scenario: str = "normal"
    profile: Literal["demo", "evidence"] = "demo"
    runs: int | None = Field(default=None, ge=1, le=2_000)
    operation_note: str | None = None
    use_agent: bool = False
    seed: int = 20260904


class MockOrderRequest(BaseModel):
    order_id: UUID
    account_id: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=1, max_length=32)
    side: Literal["buy", "sell"]
    quantity: int = Field(gt=0, le=1_000_000)
