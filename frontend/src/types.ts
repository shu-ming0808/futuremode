export type Severity = "low" | "medium" | "high" | "uncertain"

export type ScenarioId = "normal" | "high_pressure" | "downstream_bottleneck"

export type StrategyId =
  | "fixed_capacity"
  | "reactive_autoscaling"
  | "predictive_prewarm"

export const STRATEGIES: StrategyId[] = [
  "fixed_capacity",
  "reactive_autoscaling",
  "predictive_prewarm",
]

export const STRATEGY_LABEL: Record<StrategyId, string> = {
  fixed_capacity: "固定容量",
  reactive_autoscaling: "反應式擴容",
  predictive_prewarm: "預測式預熱",
}

export interface RiskEffects {
  arrival_multiplier: number | null
  worker_capacity_multiplier: number | null
  db_capacity_multiplier: number | null
  gateway_capacity_multiplier: number | null
  warmup_multiplier: number | null
  max_retries: number | null
}

export interface RiskCard {
  risk_id: string
  title: string
  severity: Severity
  matched_input_text: string
  source_url: string
  effects: RiskEffects
}

export interface StrategySummary {
  name: StrategyId
  congestion_probability: number
  congestion_probability_ci95: [number, number]
  p95_latency_ms: number
  timeout_rate: number
  database_peak_utilization: number
  gateway_peak_utilization: number
  cost: number
}

export interface CapacityRecommendation extends StrategySummary {
  workers: number
}

// ---- 單次逐 tick 模擬：後端只吐固定參數，前端 engine 自己跑 queue/pod 狀態機 ----

export interface StrategyDriveParams {
  current_workers: number
  target_workers: number
  queue_threshold: number
  queue_capacity: number
  warmup_seconds: number
  scale_down_queue_threshold: number
  scale_down_observation_seconds: number
  scale_down_confirmation_seconds: number
  scale_down_min_high_seconds: number
  scale_up_utilization_threshold: number
  scale_down_utilization_threshold: number
  warm_pool_workers: number
  warm_pool_hold_seconds: number
  controller_poll_seconds: number
}

export interface WorkerNodeParams {
  concurrency: number
  mean_service_time_seconds: number
  efficiency: number
}

export interface NodeParams {
  worker: WorkerNodeParams
  db_capacity_per_tick: number
  gateway_capacity_per_tick: number
}

export interface Recommendation {
  recommended: CapacityRecommendation | null
  warning_code: "no_feasible_plan" | null
  risks: RiskCard[]
  dt_seconds: number
  strategies: Record<StrategyId, StrategyDriveParams>
  node_params: NodeParams
}

export interface RecommendationRequest {
  scenario: ScenarioId
}

export interface ArrivalTraceResponse {
  label: string
  dt_seconds: number
  duration_seconds: number
  arrivals: number[]
}

// 前端自組給 SingleRunEngine 用的模擬輸入：arrivals 來自 /api/arrival-trace，
// strategies/node_params 來自 /api/recommendation（風險調整後的固定參數）。
export interface SingleRunTraceResponse {
  label: string
  dt_seconds: number
  arrivals: number[]
  strategies: Record<StrategyId, StrategyDriveParams>
  node_params: NodeParams
}
