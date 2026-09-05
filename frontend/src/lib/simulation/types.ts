export interface PodState {
  index: number
  busy: boolean
  remainingTime: number // 剩餘處理秒數，busy=false 時為 0
  taskId: string | null
}

export type TaskStatus =
  | "queued" // 排隊等待 pod 處理
  | "processing" // 正在被某個 pod 處理
  | "accepted" // SLO 內完成
  | "accepted_after_timeout" // 逾時後才處理完（仍視為完成）
  | "dropped" // 佇列容量不足被丟棄
  | "unresolved" // 模擬結束時仍在佇列或處理中，最終狀態未知

export interface Task {
  id: string
  arrivalTick: number
  attempt: number
  queuedAt: number
  startedAt: number | null
  finishedAt: number | null
  timedOut: boolean
  status: TaskStatus
}

/** 兩段式縮容控制器的狀態，語意同 budget_experiment.py 的 stage。 */
export type ControllerStage = "base" | "warming" | "burst" | "warm_pool"

export interface NodeMetrics {
  queueLength: number
  queueCapacity: number
  currentWorkers: number
  busyPods: number
  totalPods: number // worker 數 × concurrency，即實際並行 slot 數
  // 每台 worker 各自的 slot 佔用率（0~1），長度 = currentWorkers，供 flow 圖一格一台呈現。
  workerUtilizations: number[]
  controllerStage: ControllerStage
  observedUtilization: number // 滾動窗到達量 / 名目容量，控制器的第二訊號
  dbUtilization: number
  gatewayUtilization: number
  acceptedDelta: number // 本 tick 新完成（含逾時後補收）的請求數
  timeoutDelta: number // 本 tick 新逾時的請求數
  droppedDelta: number // 本 tick 因佇列容量不足被丟棄的請求數
}

export interface EngineFinalStats {
  p95LatencyMs: number
  timeoutRate: number
  congested: boolean
}

export interface EngineTickResult {
  tick: number
  arrivals: number
  metrics: NodeMetrics
}

/** 每個事件（task）最終落地的統計，供逐請求層級的檢視/動畫使用。 */
export interface TaskOutcome {
  id: string
  arrivalTick: number
  attempt: number
  status: TaskStatus
  waitMs: number // queuedAt -> startedAt（未開始處理則到 finishedAt/結束）
  serviceMs: number // startedAt -> finishedAt
  latencyMs: number // arrivalTick -> finishedAt（端到端）
  timedOut: boolean
}

export interface EngineOutcomeSummary {
  outcomes: TaskOutcome[]
  countsByStatus: Record<TaskStatus, number>
}
