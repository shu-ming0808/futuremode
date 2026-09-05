import type { Recommendation, ScenarioId, StrategyDriveParams, StrategyId } from "@/types"
import { STRATEGIES } from "@/types"

const SCENARIO_DEFAULTS: Record<
  ScenarioId,
  {
    current: number
    target: number
    queueThreshold: number
    queueCapacity: number
    scaleDownQueueThreshold: number
    warmPoolWorkers: number
  }
> = {
  normal: { current: 6, target: 10, queueThreshold: 300, queueCapacity: 12000, scaleDownQueueThreshold: 60, warmPoolWorkers: 8 },
  high_pressure: { current: 6, target: 14, queueThreshold: 300, queueCapacity: 12000, scaleDownQueueThreshold: 60, warmPoolWorkers: 10 },
  downstream_bottleneck: { current: 6, target: 16, queueThreshold: 250, queueCapacity: 12000, scaleDownQueueThreshold: 50, warmPoolWorkers: 11 },
}

// 縮容控制器參數，三情境共用，對應後端 scenarios/*.json 的 capacity 區塊。
const CONTROLLER_DEFAULTS = {
  scale_down_observation_seconds: 45,
  scale_down_confirmation_seconds: 10,
  scale_down_min_high_seconds: 30,
  scale_up_utilization_threshold: 0.85,
  scale_down_utilization_threshold: 0.55,
  warm_pool_hold_seconds: 20,
  controller_poll_seconds: 1.0,
}

// db/gateway 每 tick 容量對應後端 scenarios/*.json：
// db_connections × db_rps_per_connection × DT，gateway_rps × DT。
const NODE_CAPACITY: Record<ScenarioId, { db: number; gateway: number }> = {
  normal: { db: 120, gateway: 120 },
  high_pressure: { db: 120, gateway: 120 },
  downstream_bottleneck: { db: 54, gateway: 56 },
}

// 三策略在各情境下的實測 Monte Carlo 結果（uv run compare_strategies，300 runs/情境，
// scenarios/*.json 原始參數，未套風險調整），用來在 demo 卡片上凸顯策略間差異：
// normal 三策略幾乎打平；high_pressure 只有 predictive_prewarm 不壅塞；
// downstream_bottleneck 因下游硬上限三者全壅塞，僅 p95 有梯度差異，故無可行方案。
const STRATEGY_METRICS: Record<
  ScenarioId,
  Record<
    StrategyId,
    {
      congestion_probability: number
      p95_latency_ms: number
      timeout_rate: number
      database_peak_utilization: number
      gateway_peak_utilization: number
      cost: number
    }
  >
> = {
  normal: {
    fixed_capacity: { congestion_probability: 0.0, p95_latency_ms: 100, timeout_rate: 0.0, database_peak_utilization: 0.37, gateway_peak_utilization: 0.37, cost: 30 },
    reactive_autoscaling: { congestion_probability: 0.0, p95_latency_ms: 100, timeout_rate: 0.0, database_peak_utilization: 0.37, gateway_peak_utilization: 0.37, cost: 30 },
    predictive_prewarm: { congestion_probability: 0.0, p95_latency_ms: 100, timeout_rate: 0.0, database_peak_utilization: 0.38, gateway_peak_utilization: 0.38, cost: 50 },
  },
  high_pressure: {
    fixed_capacity: { congestion_probability: 0.827, p95_latency_ms: 25000, timeout_rate: 0.5266, database_peak_utilization: 0.40, gateway_peak_utilization: 0.40, cost: 30 },
    reactive_autoscaling: { congestion_probability: 0.827, p95_latency_ms: 14900, timeout_rate: 0.5992, database_peak_utilization: 0.89, gateway_peak_utilization: 0.89, cost: 52 },
    predictive_prewarm: { congestion_probability: 0.0, p95_latency_ms: 100, timeout_rate: 0.0, database_peak_utilization: 0.82, gateway_peak_utilization: 0.82, cost: 70 },
  },
  downstream_bottleneck: {
    fixed_capacity: { congestion_probability: 1.0, p95_latency_ms: 25000, timeout_rate: 0.5003, database_peak_utilization: 0.89, gateway_peak_utilization: 0.86, cost: 30 },
    reactive_autoscaling: { congestion_probability: 1.0, p95_latency_ms: 23100, timeout_rate: 0.5259, database_peak_utilization: 1.0, gateway_peak_utilization: 0.96, cost: 75 },
    predictive_prewarm: { congestion_probability: 1.0, p95_latency_ms: 22300, timeout_rate: 0.5257, database_peak_utilization: 1.0, gateway_peak_utilization: 0.96, cost: 80 },
  },
}

// 後端沒開時的 fallback，確保 demo 版面隨時完整可視。
export function buildMockAssessment(scenario: ScenarioId): Recommendation {
  const bottleneck = scenario === "downstream_bottleneck"
  const defaults = SCENARIO_DEFAULTS[scenario]
  // 預測式預熱一開始就直接開到情境的 target_workers，不像 reactive 要等 warmup。
  const predictiveWorkers = defaults.target
  const metrics = STRATEGY_METRICS[scenario]
  const strategies = Object.fromEntries(
    STRATEGIES.map((strategy) => {
      const targetWorkers =
        strategy === "predictive_prewarm" ? predictiveWorkers : defaults.target
      const params: StrategyDriveParams = {
        current_workers: defaults.current,
        target_workers: targetWorkers,
        queue_threshold: defaults.queueThreshold,
        queue_capacity: defaults.queueCapacity,
        warmup_seconds: 30,
        scale_down_queue_threshold: defaults.scaleDownQueueThreshold,
        warm_pool_workers: Math.min(defaults.warmPoolWorkers, targetWorkers),
        ...CONTROLLER_DEFAULTS,
      }
      return [strategy, params]
    })
  ) as Record<StrategyId, StrategyDriveParams>
  const predictive = metrics.predictive_prewarm
  return {
    recommended: bottleneck
      ? null
      : {
          name: "predictive_prewarm",
          workers: predictiveWorkers,
          congestion_probability: predictive.congestion_probability,
          congestion_probability_ci95: [0.0, 0.008] as [number, number],
          p95_latency_ms: predictive.p95_latency_ms,
          timeout_rate: predictive.timeout_rate,
          database_peak_utilization: predictive.database_peak_utilization,
          gateway_peak_utilization: predictive.gateway_peak_utilization,
          cost: predictive.cost,
        },
    warning_code: bottleneck ? "no_feasible_plan" : null,
    dt_seconds: 0.1,
    strategies,
    node_params: {
      worker: { concurrency: 20, mean_service_time_seconds: 0.2, efficiency: 0.8 },
      db_capacity_per_tick: NODE_CAPACITY[scenario].db,
      gateway_capacity_per_tick: NODE_CAPACITY[scenario].gateway,
    },
    // risk_id/title/source_url 取自後端真實目錄
    // backend/openingguard/data/risk_catalog.json，僅 severity/matched_input_text/effects 為假資料示意。
    risks: [
      {
        risk_id: "release_product_file_download",
        title: "盤前版本或商品檔更新形成共享資源壓力",
        severity: "medium",
        matched_input_text: "今晚部署新版下單服務",
        source_url:
          "https://m.esunsec.com.tw/news/instant-detail.aspx?id=%7B910B8C5A-40C3-4E7A-AFA9-A970BE30542D%7D",
        effects: {
          arrival_multiplier: 1.2,
          worker_capacity_multiplier: null,
          db_capacity_multiplier: null,
          gateway_capacity_multiplier: null,
          warmup_multiplier: null,
          max_retries: null,
        },
      },
      {
        risk_id: "database_connection_saturation",
        title: "Database connection pool 飽和",
        severity: bottleneck ? "high" : "low",
        matched_input_text: bottleneck ? "交易閘道近期維護後容量下修" : "",
        source_url: "https://www.cna.com.tw/news/afe/202408050100.aspx",
        effects: {
          arrival_multiplier: null,
          worker_capacity_multiplier: null,
          db_capacity_multiplier: bottleneck ? 0.8 : null,
          gateway_capacity_multiplier: null,
          warmup_multiplier: null,
          max_retries: null,
        },
      },
    ],
  }
}
