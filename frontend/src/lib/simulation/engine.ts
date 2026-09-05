import { sampleExponential } from "./distributions"
import type {
  ControllerStage,
  EngineFinalStats,
  EngineOutcomeSummary,
  EngineTickResult,
  NodeMetrics,
  PodState,
  Task,
  TaskOutcome,
  TaskStatus,
} from "./types"
import type { NodeParams, StrategyDriveParams, StrategyId } from "@/types"

const SLO_LATENCY_SECONDS = 2
const MAX_TIMEOUT_RATE = 0.001
const DOWNSTREAM_SAFE_UTILIZATION = 0.95
const ROLLING_WINDOW_TICKS = 30 // 3 秒（0.1s/tick），同後端 core.py 的滾動窗口

/**
 * 前端獨立跑一份 queue/pod 狀態機，只吃後端的 arrivals[] 與固定情境參數，
 * 不 replay 後端算好的結果（見 backend/docs/前端需求-單次逐tick模擬.md 新契約）。
 *
 * 每個到達的請求都是一個獨立 Task 物件（而非批量計數），逐 tick 追蹤其
 * queued -> processing -> accepted/timeout/dropped 生命週期，
 * 讓 UI 能畫出個別事件顆粒動畫，並統計每個事件最終落地的狀態與耗時。
 *
 * 吞吐量由 pod 的實際佔用決定：每個 worker 有 concurrency 條 slot，
 * 一條 slot 抓一個 task 並佔用一段服務時間才釋放。因此 busyPods 是真實佔用比例，
 * 而非依處理量回推的視覺值；worker 數不同的策略會自然分化出不同的 queue 長度與延遲。
 */
export class SingleRunEngine {
  private readonly strategy: StrategyId
  private readonly arrivals: number[]
  private readonly params: StrategyDriveParams
  private readonly nodeParams: NodeParams
  private readonly dt: number

  private tick = 0
  private taskCounter = 0
  private currentWorkers: number
  private stage: ControllerStage
  private scaleReadyTick: number | null = null
  private burstReadyTick: number | null = null
  private warmPoolEnterTick: number | null = null
  private lowLoadSinceTick: number | null = null
  private readonly warmPoolWorkers: number
  private readonly observationTicks: number
  private readonly confirmationTicks: number
  private readonly minHighTicks: number
  private readonly warmPoolHoldTicks: number
  private readonly controllerPollTicks: number
  // 滾動窗內的到達量與可服務量，比值即 observed utilization，作為擴縮容的第二訊號。
  private loadWindow: { arrivals: number; capacity: number }[] = []
  private windowArrivals = 0
  private windowCapacity = 0
  private observedUtilization = 0
  private pods: PodState[]
  private queue: Task[] = []
  private readonly tasks = new Map<string, Task>()

  private totalOriginalRequests = 0
  private totalAttempts = 0
  private totalTimedOut = 0
  private maxQueueLen = 0
  private dbPeak = 0
  private gatewayPeak = 0
  private latencySamples: number[] = []
  private rollingLatencies: { tick: number; latencyMs: number }[] = []

  constructor(
    strategy: StrategyId,
    arrivals: number[],
    params: StrategyDriveParams,
    nodeParams: NodeParams,
    dt: number
  ) {
    this.strategy = strategy
    this.arrivals = arrivals
    this.params = params
    this.nodeParams = nodeParams
    this.dt = dt
    const prewarmed = strategy === "predictive_prewarm"
    this.currentWorkers = prewarmed ? params.target_workers : params.current_workers
    // 預熱策略開場即在高容量，直接進 burst 讓 min-hold 從 tick 0 起算，
    // 否則 stage 停在 base，尖峰過後永遠不會觸發縮容。
    this.stage = prewarmed ? "burst" : "base"
    this.burstReadyTick = prewarmed ? 0 : null
    this.warmPoolWorkers = Math.min(params.warm_pool_workers, params.target_workers)
    const toTicks = (seconds: number) => Math.max(1, Math.round(seconds / dt))
    this.observationTicks = toTicks(params.scale_down_observation_seconds)
    this.confirmationTicks = Math.round(params.scale_down_confirmation_seconds / dt)
    this.minHighTicks = Math.round(params.scale_down_min_high_seconds / dt)
    this.warmPoolHoldTicks = Math.round(params.warm_pool_hold_seconds / dt)
    this.controllerPollTicks = toTicks(params.controller_poll_seconds)
    this.pods = this.buildPods(this.currentWorkers)
  }

  /** 一個 pod 代表一台 worker 上的一條並行 slot，故 pod 數 = workers × concurrency。 */
  private buildPods(workers: number): PodState[] {
    const slots = Math.max(1, Math.round(workers * this.nodeParams.worker.concurrency))
    return Array.from({ length: slots }, (_, i) => ({
      index: i,
      busy: false,
      remainingTime: 0,
      taskId: null,
    }))
  }

  /**
   * 擴縮容時保留既有 pod 的佔用狀態，只增刪尾端 slot，
   * 否則整批重建會憑空清掉手上正在處理的 task，讓吞吐量無中生有跳一拍。
   */
  private resizePods(workers: number): void {
    const slots = Math.max(1, Math.round(workers * this.nodeParams.worker.concurrency))
    if (slots === this.pods.length) return
    if (slots > this.pods.length) {
      for (let i = this.pods.length; i < slots; i += 1) {
        this.pods.push({ index: i, busy: false, remainingTime: 0, taskId: null })
      }
      return
    }
    // 縮容：優先移除閒置 slot，仍在處理的 task 讓它跑完再退場，避免請求平白蒸發。
    const kept = this.pods.filter((pod) => pod.busy)
    const idle = this.pods.filter((pod) => !pod.busy)
    const next = kept.concat(idle).slice(0, Math.max(slots, kept.length))
    next.forEach((pod, i) => {
      pod.index = i
    })
    this.pods = next
  }

  /** 把忙碌 slot 平均攤回每台 worker，回傳各台 0~1 的佔用率。 */
  private workerUtilizations(busyPods: number): number[] {
    const workers = Math.max(1, this.currentWorkers)
    const slotsPerWorker = this.pods.length / workers
    if (slotsPerWorker <= 0) return Array.from({ length: workers }, () => 0)
    const base = Math.floor(busyPods / workers)
    const remainder = busyPods % workers
    return Array.from({ length: workers }, (_, i) =>
      Math.min(1, (base + (i < remainder ? 1 : 0)) / slotsPerWorker)
    )
  }

  private newTaskId(): string {
    this.taskCounter += 1
    return `${this.strategy}-t${this.taskCounter}`
  }

  private enqueueOrDrop(task: Task): void {
    if (this.queue.length >= this.params.queue_capacity) {
      task.status = "dropped"
      task.finishedAt = this.tick
      this.tasks.set(task.id, task)
      return
    }
    this.tasks.set(task.id, task)
    this.queue.push(task)
  }

  get totalTicks(): number {
    return this.arrivals.length
  }

  get isDone(): boolean {
    return this.tick >= this.totalTicks
  }

  /** 服務一個請求平均耗費的秒數，worker_efficiency 越低等於實際服務越慢。 */
  private meanServiceSeconds(): number {
    const efficiency = Math.max(0.05, this.nodeParams.worker.efficiency)
    return this.nodeParams.worker.mean_service_time_seconds / efficiency
  }

  /** 下游每 tick 可承接的請求數，同 core.py 的 db_tick_cap / gateway_tick_cap。 */
  private downstreamCapacityPerTick(): { db: number; gateway: number } {
    return {
      db: Math.floor(this.nodeParams.db_capacity_per_tick),
      gateway: Math.floor(this.nodeParams.gateway_capacity_per_tick),
    }
  }

  /**
   * 兩段式 hysteresis 控制器，語意同 backend/openingguard/budget_experiment.py：
   * burst -> warm_pool -> base，每段各有最短保持期，且低載需連續確認才推進。
   *
   * 擴容看 queue 壓力或 utilization 任一過線；縮容則要求 queue 與 utilization
   * 同時低檔，任一 tick 回升即重置確認計時，避免尖峰餘波中反覆抖動。
   * 縮完仍可 rebound 重擴，故 warm_pool 與 base 都保留擴容入口。
   */
  private runController(queueLen: number): void {
    if (this.strategy === "fixed_capacity") return

    const windowReady = this.loadWindow.length >= this.observationTicks
    const queuePressure = queueLen >= this.params.queue_threshold
    const utilizationPressure =
      windowReady && this.observedUtilization >= this.params.scale_up_utilization_threshold
    const lowLoadSample =
      windowReady &&
      this.observedUtilization <= this.params.scale_down_utilization_threshold &&
      queueLen <= this.params.scale_down_queue_threshold

    if ((this.stage === "burst" || this.stage === "warm_pool") && lowLoadSample) {
      if (this.lowLoadSinceTick === null) this.lowLoadSinceTick = this.tick
    } else {
      this.lowLoadSinceTick = null
    }
    const lowLoadConfirmed =
      this.lowLoadSinceTick !== null && this.tick - this.lowLoadSinceTick >= this.confirmationTicks

    // warmup 進行中不再下任何新決策，等 pod 到位後由下一輪 poll 重新評估。
    if (this.stage === "warming") return
    const isPollTick = this.tick % this.controllerPollTicks === 0

    if (
      (this.stage === "base" || this.stage === "warm_pool") &&
      isPollTick &&
      (queuePressure || utilizationPressure) &&
      this.params.target_workers > this.currentWorkers
    ) {
      this.stage = "warming"
      this.scaleReadyTick = this.tick + Math.round(this.params.warmup_seconds / this.dt)
      this.lowLoadSinceTick = null
      return
    }

    if (!isPollTick) return

    if (this.stage === "burst") {
      const heldLongEnough =
        this.burstReadyTick !== null && this.tick - this.burstReadyTick >= this.minHighTicks
      if (!heldLongEnough || !lowLoadConfirmed) return
      if (this.warmPoolWorkers < this.currentWorkers) {
        this.stage = "warm_pool"
        this.warmPoolEnterTick = this.tick
        this.currentWorkers = this.warmPoolWorkers
      } else {
        this.stage = "base"
        this.currentWorkers = this.params.current_workers
      }
      this.resizePods(this.currentWorkers)
      this.lowLoadSinceTick = null
      return
    }

    if (this.stage === "warm_pool") {
      const poolHeldLongEnough =
        this.warmPoolEnterTick !== null &&
        this.tick - this.warmPoolEnterTick >= this.warmPoolHoldTicks
      if (!poolHeldLongEnough || !lowLoadConfirmed) return
      this.stage = "base"
      this.currentWorkers = this.params.current_workers
      this.resizePods(this.currentWorkers)
      this.lowLoadSinceTick = null
    }
  }

  /** 前進一個 tick，回傳這一格的節點狀態供渲染。 */
  step(): EngineTickResult {
    const tick = this.tick
    if (this.scaleReadyTick !== null && tick >= this.scaleReadyTick) {
      this.currentWorkers = this.params.target_workers
      this.resizePods(this.currentWorkers)
      this.scaleReadyTick = null
      this.stage = "burst"
      this.burstReadyTick = tick
      this.warmPoolEnterTick = null
      this.lowLoadSinceTick = null
    }

    // 先讓手上的 task 走完這一 tick 的服務時間，釋放出來的 slot 本 tick 就能接新工作。
    let acceptedDelta = 0
    for (const pod of this.pods) {
      if (!pod.busy) continue
      pod.remainingTime -= this.dt
      if (pod.remainingTime > 0) continue
      const task = pod.taskId ? this.tasks.get(pod.taskId) : undefined
      if (task) {
        task.finishedAt = tick
        task.status = task.timedOut ? "accepted_after_timeout" : "accepted"
        const latencyMs = (tick - task.arrivalTick + 1) * this.dt * 1000
        this.latencySamples.push(latencyMs)
        this.rollingLatencies.push({ tick, latencyMs })
        acceptedDelta += 1
      }
      pod.busy = false
      pod.remainingTime = 0
      pod.taskId = null
    }

    const arrivals = this.arrivals[tick] ?? 0
    let droppedDelta = 0
    if (arrivals > 0) {
      this.totalOriginalRequests += arrivals
      this.totalAttempts += arrivals
      for (let i = 0; i < arrivals; i += 1) {
        const task: Task = {
          id: this.newTaskId(),
          arrivalTick: tick,
          attempt: 0,
          queuedAt: tick,
          startedAt: null,
          finishedAt: null,
          timedOut: false,
          status: "queued",
        }
        this.enqueueOrDrop(task)
        if (task.status === "dropped") droppedDelta += 1
      }
    }

    // 逾時只標記不取消：後端仍在處理，故 timed-out 的 task 留在佇列直到被服務完。
    let timeoutDelta = 0
    const sloTicks = Math.max(1, Math.round(SLO_LATENCY_SECONDS / this.dt))
    for (const task of this.queue) {
      if (!task.timedOut && tick - task.queuedAt >= sloTicks) {
        task.timedOut = true
        timeoutDelta += 1
      }
    }
    for (const pod of this.pods) {
      if (!pod.busy || !pod.taskId) continue
      const task = this.tasks.get(pod.taskId)
      if (task && !task.timedOut && tick - task.queuedAt >= sloTicks) {
        task.timedOut = true
        timeoutDelta += 1
      }
    }
    this.totalTimedOut += timeoutDelta

    const queueLen = this.queue.length
    this.maxQueueLen = Math.max(this.maxQueueLen, queueLen)
    // 控制器吃的是上一 tick 為止的滾動窗，本 tick 的容量還沒算出來；
    // 一格延遲對 30 tick 的觀察窗無實質影響，但可讓決策與 resize 在同一 tick 生效。
    this.runController(queueLen)

    // 本 tick 能開工的量同時受空閒 slot 與下游容量夾擊，三者取最小值。
    const { db, gateway } = this.downstreamCapacityPerTick()
    const freeSlots = this.pods.reduce((n, pod) => n + (pod.busy ? 0 : 1), 0)
    const admitCapacity = Math.max(0, Math.min(freeSlots, db, gateway))
    // Utilization 分母用「所有 slot 全空時的理論吞吐」而非本 tick 剩餘的空閒 slot，
    // 否則滿載時 freeSlots→0 會讓比值爆炸，反而讀成需要擴容。
    const nominalCapacity = Math.max(0, Math.min(this.pods.length, db, gateway))
    this.loadWindow.push({ arrivals, capacity: nominalCapacity })
    this.windowArrivals += arrivals
    this.windowCapacity += nominalCapacity
    if (this.loadWindow.length > this.observationTicks) {
      const old = this.loadWindow.shift()!
      this.windowArrivals -= old.arrivals
      this.windowCapacity -= old.capacity
    }
    this.observedUtilization = this.windowCapacity > 0 ? this.windowArrivals / this.windowCapacity : 0

    let toStart = Math.min(queueLen, admitCapacity)
    const startedCount = toStart
    const meanService = this.meanServiceSeconds()
    for (const pod of this.pods) {
      if (toStart <= 0) break
      if (pod.busy) continue
      const task = this.queue.shift()
      if (!task) break
      task.startedAt = tick
      task.status = "processing"
      pod.busy = true
      pod.taskId = task.id
      // 服務時間指數分布：讓 pod 釋放時機分散，避免整批 slot 同進同出的鋸齒。
      pod.remainingTime = Math.max(this.dt, sampleExponential(1 / meanService))
      toStart -= 1
    }

    if (db > 0) this.dbPeak = Math.max(this.dbPeak, startedCount / db)
    if (gateway > 0) this.gatewayPeak = Math.max(this.gatewayPeak, startedCount / gateway)

    while (
      this.rollingLatencies.length > 0 &&
      this.rollingLatencies[0].tick <= tick - ROLLING_WINDOW_TICKS
    ) {
      this.rollingLatencies.shift()
    }

    const busyPods = this.pods.reduce((n, pod) => n + (pod.busy ? 1 : 0), 0)
    // resizePods 會把 busy slot 排到陣列前段，故不能按位置切段當作「第 n 台 worker」，
    // 否則前幾台永遠滿載、後幾台永遠全閒。所有 worker 規格相同，改為把忙碌 slot
    // 均分回各台：先讓每台填滿一輪，餘數再分給前面幾台。
    const workerUtilizations = this.workerUtilizations(busyPods)

    const metrics: NodeMetrics = {
      queueLength: this.queue.length,
      queueCapacity: this.params.queue_capacity,
      currentWorkers: this.currentWorkers,
      busyPods,
      totalPods: this.pods.length,
      workerUtilizations,
      controllerStage: this.stage,
      observedUtilization: this.observedUtilization,
      dbUtilization: db > 0 ? startedCount / db : 0,
      gatewayUtilization: gateway > 0 ? startedCount / gateway : 0,
      acceptedDelta,
      timeoutDelta,
      droppedDelta,
    }

    this.tick += 1
    return { tick, arrivals, metrics }
  }

  get rollingP95Ms(): number {
    if (this.rollingLatencies.length === 0) return 0
    const sorted = [...this.rollingLatencies].map((s) => s.latencyMs).sort((a, b) => a - b)
    const idx = Math.min(sorted.length - 1, Math.ceil(sorted.length * 0.95) - 1)
    return sorted[idx]
  }

  finalStats(): EngineFinalStats {
    const sorted = [...this.latencySamples].sort((a, b) => a - b)
    const idx = sorted.length ? Math.min(sorted.length - 1, Math.ceil(sorted.length * 0.95) - 1) : -1
    const p95LatencyMs = idx >= 0 ? sorted[idx] : 0
    const timeoutRate = this.totalAttempts ? this.totalTimedOut / this.totalAttempts : 0
    const congested =
      p95LatencyMs >= SLO_LATENCY_SECONDS * 1000 ||
      timeoutRate >= MAX_TIMEOUT_RATE ||
      this.maxQueueLen >= this.params.queue_capacity ||
      this.dbPeak >= DOWNSTREAM_SAFE_UTILIZATION ||
      this.gatewayPeak >= DOWNSTREAM_SAFE_UTILIZATION
    return { p95LatencyMs, timeoutRate, congested }
  }

  /** 每個事件（task）最終落地的統計；仍在佇列/處理中的任務標記為 unresolved。 */
  outcomes(): EngineOutcomeSummary {
    const countsByStatus: Record<TaskStatus, number> = {
      queued: 0,
      processing: 0,
      accepted: 0,
      accepted_after_timeout: 0,
      dropped: 0,
      unresolved: 0,
    }
    const outcomes: TaskOutcome[] = []
    for (const task of this.tasks.values()) {
      const status: TaskStatus =
        task.status === "queued" || task.status === "processing" ? "unresolved" : task.status
      const finishedAt = task.finishedAt ?? this.tick
      const waitMs = ((task.startedAt ?? finishedAt) - task.queuedAt) * this.dt * 1000
      const serviceMs = task.startedAt !== null ? (finishedAt - task.startedAt) * this.dt * 1000 : 0
      const latencyMs = (finishedAt - task.arrivalTick) * this.dt * 1000
      countsByStatus[status] += 1
      outcomes.push({
        id: task.id,
        arrivalTick: task.arrivalTick,
        attempt: task.attempt,
        status,
        waitMs,
        serviceMs,
        latencyMs,
        timedOut: task.timedOut,
      })
    }
    return { outcomes, countsByStatus }
  }
}
