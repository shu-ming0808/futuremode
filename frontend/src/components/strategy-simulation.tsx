import { useEffect, useMemo, useRef, useState } from "react"
import { Pause, Play, RotateCcw } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Slider } from "@/components/ui/slider"
import { PipelineFlow } from "@/components/pipeline-flow"
import { TraceTimeline, type TimelineRow } from "@/components/trace-timeline"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { STRATEGIES, STRATEGY_LABEL } from "@/types"
import type { SingleRunTraceResponse, StrategyId } from "@/types"
import { SingleRunEngine } from "@/lib/simulation/engine"
import type { NodeMetrics } from "@/lib/simulation/types"

const PLAYBACK_DURATION_MS = 15_000

interface StrategyRuntime {
  engine: SingleRunEngine
  metrics: NodeMetrics | null
  acceptedCumulative: number
  timeoutCumulative: number
}

function buildRuntime(data: SingleRunTraceResponse, strategy: StrategyId): StrategyRuntime {
  return {
    engine: new SingleRunEngine(
      strategy,
      data.arrivals,
      data.strategies[strategy],
      data.node_params,
      data.dt_seconds
    ),
    metrics: null,
    acceptedCumulative: 0,
    timeoutCumulative: 0,
  }
}

export function StrategySimulation({ data }: { data: SingleRunTraceResponse }) {
  const maxTick = data.arrivals.length - 1
  const [tick, setTick] = useState(0)
  const [playing, setPlaying] = useState(true)
  const [activeStrategy, setActiveStrategy] = useState<StrategyId>("predictive_prewarm")
  const [runtimes, setRuntimes] = useState<Record<StrategyId, StrategyRuntime>>(() =>
    Object.fromEntries(STRATEGIES.map((s) => [s, buildRuntime(data, s)])) as Record<
      StrategyId,
      StrategyRuntime
    >
  )
  const [rows, setRows] = useState<TimelineRow[]>([])
  const rowsBufferRef = useRef<TimelineRow[]>([])
  const lastFlushRef = useRef(0)
  const rafRef = useRef<number | null>(null)
  const startRef = useRef<{ time: number; tick: number } | null>(null)
  const appliedTickRef = useRef(-1)
  // rAF 迴圈內純計算用的最新 runtimes 快照，避免每個動畫幀都要透過 setState 讀寫。
  const runtimesRef = useRef<Record<StrategyId, StrategyRuntime> | null>(null)
  const runtimesSnapshotRef = useRef(runtimes)
  useEffect(() => {
    runtimesSnapshotRef.current = runtimes
  }, [runtimes])

  // 時間軸只保留抽稀後的取樣點，避免 3000+ tick 全塞進 recharts 導致每幀重繪整條線。
  const ROWS_SAMPLE_STRIDE = 5
  // rows/runtimes state 節流間隔：tick 每個 rAF 幀（~16ms）都會推進，但 UI re-render
  // 節流到約 10Hz，避免每幀都 setState 造成 PipelineFlow/圖表反覆重繪、掉幀甚至畫面空白。
  const UI_FLUSH_INTERVAL_MS = 100

  // 重建全部 engine／清空累積數據與時間軸緩衝，情境切換與「重播」共用同一份重置邏輯，
  // 否則重播只重置 tick 而 engine 內部狀態、rows、appliedTickRef 仍是上一輪跑到底的值，
  // advanceTo() 會因 endTick(從 0 起算) <= startTick(舊的 maxTick) 直接跳過，
  // 新一輪不會真的重算，圖表在新舊兩輪資料交界處出現非遞增時間軸，呈現階段性下墜。
  function resetRuntimes() {
    setRuntimes(
      Object.fromEntries(STRATEGIES.map((s) => [s, buildRuntime(data, s)])) as Record<
        StrategyId,
        StrategyRuntime
      >
    )
    setRows([])
    rowsBufferRef.current = []
    lastFlushRef.current = 0
    appliedTickRef.current = -1
    runtimesRef.current = null
    startRef.current = null
  }

  // 情境或建議 worker 數變動時（key 已含在 App.tsx 的 key= 裡強制整包重掛），重建 engine。
  useEffect(() => {
    resetRuntimes()
    setTick(0)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data])

  // 把某段 [startTick, endTick) 的 tick 推進交給 engine，純計算不觸發 setState；
  // 呼叫端（rAF 迴圈）自行決定多久才把結果 flush 進 React state 一次。
  function advanceTo(endTick: number, current: Record<StrategyId, StrategyRuntime>) {
    const startTick = appliedTickRef.current
    if (endTick <= startTick) return current
    const next: Record<StrategyId, StrategyRuntime> = { ...current }
    // 三策略必須同步逐 tick 前進（而非各自跑到底再合併），時間軸每列才對得上同一個 tick。
    for (let stepped = startTick; stepped < endTick; stepped += 1) {
      let rowTime = 0
      let rowArrivals = 0
      const rowValues: Partial<
        Record<
          | `queue_${StrategyId}`
          | `p95_${StrategyId}`
          | `accepted_${StrategyId}`
          | `timeout_${StrategyId}`
          | `pods_${StrategyId}`
          | `busy_pods_${StrategyId}`,
          number
        >
      > = {}
      for (const strategy of STRATEGIES) {
        const runtime = next[strategy]
        if (runtime.engine.isDone) {
          // 策略已跑完（三策略 arrivals 長度相同，理論同步結束，但保留防呆）：
          // 沿用累積值而非省略 key，否則抽稀取樣點少了這個 key，Recharts 會把
          // 該點畫成斷線／掉到 0，圖表上出現不存在的「階段性下墜」。
          rowValues[`accepted_${strategy}`] = runtime.acceptedCumulative
          rowValues[`timeout_${strategy}`] = runtime.timeoutCumulative
          rowValues[`queue_${strategy}`] = runtime.metrics?.queueLength ?? 0
          rowValues[`p95_${strategy}`] = runtime.engine.rollingP95Ms
          rowValues[`pods_${strategy}`] = runtime.metrics?.totalPods ?? 0
          rowValues[`busy_pods_${strategy}`] = runtime.metrics?.busyPods ?? 0
          continue
        }
        const result = runtime.engine.step()
        const acceptedCumulative = runtime.acceptedCumulative + result.metrics.acceptedDelta
        const timeoutCumulative = runtime.timeoutCumulative + result.metrics.timeoutDelta
        next[strategy] = {
          engine: runtime.engine,
          metrics: result.metrics,
          acceptedCumulative,
          timeoutCumulative,
        }
        rowTime = Math.round(result.tick * data.dt_seconds * 10) / 10
        rowArrivals = result.arrivals
        rowValues[`queue_${strategy}`] = result.metrics.queueLength
        rowValues[`p95_${strategy}`] = runtime.engine.rollingP95Ms
        rowValues[`accepted_${strategy}`] = acceptedCumulative
        rowValues[`timeout_${strategy}`] = timeoutCumulative
        rowValues[`pods_${strategy}`] = result.metrics.totalPods
        rowValues[`busy_pods_${strategy}`] = result.metrics.busyPods
      }
      if (stepped % ROWS_SAMPLE_STRIDE === 0) {
        rowsBufferRef.current.push({ time: rowTime, arrivals: rowArrivals, ...rowValues } as TimelineRow)
      }
    }
    appliedTickRef.current = endTick
    return next
  }

  // 拖曳 slider 往回頭時不重算，只是暫停在較早畫面（引擎狀態不支援倒帶）；
  // 往前拖曳（暫停狀態下）則立即推進並同步渲染，不等節流。
  useEffect(() => {
    if (playing) return
    if (tick <= appliedTickRef.current) return
    setRuntimes((prev) => {
      const next = advanceTo(tick, prev)
      runtimesRef.current = next
      if (rowsBufferRef.current.length > 0) {
        const buffered = rowsBufferRef.current
        rowsBufferRef.current = []
        setRows((prevRows) => [...prevRows, ...buffered])
      }
      return next
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick, playing])

  // 播放中：單一 rAF 迴圈同時負責推進 engine（純計算）與節流 flush 到 React state
  // （約 10Hz），避免每個 ~16ms 的動畫幀都 setState 造成 PipelineFlow/圖表反覆重繪。
  useEffect(() => {
    if (!playing) {
      startRef.current = null
      return
    }
    const ticksPerMs = maxTick / PLAYBACK_DURATION_MS

    const step = (now: number) => {
      if (!startRef.current) {
        startRef.current = { time: now, tick: appliedTickRef.current }
      }
      const elapsed = now - startRef.current.time
      const nextTick = Math.min(
        maxTick,
        Math.round(startRef.current.tick + elapsed * ticksPerMs)
      )

      const current = runtimesRef.current ?? runtimesSnapshotRef.current
      const advanced = advanceTo(nextTick, current)
      runtimesRef.current = advanced

      const isFinalTick = nextTick >= maxTick
      if (isFinalTick || now - lastFlushRef.current >= UI_FLUSH_INTERVAL_MS) {
        lastFlushRef.current = now
        setTick(nextTick)
        setRuntimes(advanced)
        if (rowsBufferRef.current.length > 0) {
          const buffered = rowsBufferRef.current
          rowsBufferRef.current = []
          setRows((prevRows) => [...prevRows, ...buffered])
        }
      }

      if (isFinalTick) {
        setPlaying(false)
        return
      }
      rafRef.current = requestAnimationFrame(step)
    }
    rafRef.current = requestAnimationFrame(step)

    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, maxTick])

  const finals = useMemo(
    () =>
      Object.fromEntries(
        STRATEGIES.map((s) => [s, runtimes[s].engine.finalStats()])
      ) as Record<StrategyId, ReturnType<SingleRunEngine["finalStats"]>>,
    [runtimes]
  )

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <Button
          size="icon"
          variant="outline"
          onClick={() => {
            if (tick >= maxTick) {
              resetRuntimes()
              setTick(0)
              setPlaying(true)
              return
            }
            setPlaying((p) => !p)
          }}
        >
          {playing ? <Pause /> : tick >= maxTick ? <RotateCcw /> : <Play />}
        </Button>
        <Button
          size="icon"
          variant="ghost"
          onClick={() => {
            resetRuntimes()
            setTick(0)
            setPlaying(false)
          }}
        >
          <RotateCcw />
        </Button>
        <Slider
          className="flex-1"
          min={0}
          max={maxTick}
          step={1}
          value={[tick]}
          onValueChange={(value) => {
            setPlaying(false)
            setTick(Array.isArray(value) ? value[0] : value)
          }}
        />
        <span className="w-24 shrink-0 text-right text-xs text-muted-foreground tabular-nums">
          {(tick * data.dt_seconds).toFixed(1)}s / {(maxTick * data.dt_seconds).toFixed(1)}s
        </span>
      </div>

      <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
        {STRATEGIES.map((strategy) => {
          const runtime = runtimes[strategy]
          const final = finals[strategy]
          return (
            <button
              key={strategy}
              type="button"
              onClick={() => setActiveStrategy(strategy)}
              className={`rounded-md border p-3 text-left transition-colors ${
                activeStrategy === strategy ? "border-primary bg-muted/50" : "border-border"
              }`}
            >
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium">{STRATEGY_LABEL[strategy]}</span>
                <Badge variant={final.congested ? "destructive" : "secondary"}>
                  {final.congested ? "壅塞" : "順暢"}
                </Badge>
              </div>
              <div className="mt-1 text-2xl font-semibold tabular-nums">
                {(final.timeoutRate * 100).toFixed(2)}
                <span className="text-xs font-normal text-muted-foreground">% 逾時率</span>
              </div>
              <div className="mt-1 flex justify-between text-xs text-muted-foreground tabular-nums">
                <span>
                  {runtime.metrics?.currentWorkers ?? data.strategies[strategy].current_workers} 台
                  worker
                </span>
                <span>已完成 {runtime.acceptedCumulative}</span>
              </div>
            </button>
          )
        })}
      </div>

      <Tabs
        value={activeStrategy}
        onValueChange={(value) => setActiveStrategy(value as StrategyId)}
      >
        <TabsList>
          {STRATEGIES.map((strategy) => (
            <TabsTrigger key={strategy} value={strategy}>
              {STRATEGY_LABEL[strategy]}
              {finals[strategy].congested && (
                <Badge variant="destructive" className="ml-1.5">
                  壅塞
                </Badge>
              )}
            </TabsTrigger>
          ))}
        </TabsList>
        {STRATEGIES.map((strategy) => {
          const runtime = runtimes[strategy]
          const final = finals[strategy]
          return (
            <TabsContent key={strategy} value={strategy}>
              <Card>
                <CardHeader className="flex flex-row items-center justify-between gap-2">
                  <CardTitle className="text-base">
                    {STRATEGY_LABEL[strategy]}（
                    {runtime.metrics?.currentWorkers ?? data.strategies[strategy].current_workers} 台）
                  </CardTitle>
                  <Badge variant={final.congested ? "destructive" : "secondary"}>
                    {final.congested ? "壅塞" : "順暢"}
                  </Badge>
                </CardHeader>
                <CardContent>
                  <PipelineFlow
                    metrics={runtime.metrics}
                    acceptedCumulative={runtime.acceptedCumulative}
                    timeoutCumulative={runtime.timeoutCumulative}
                  />
                  <div className="mt-2 flex justify-between text-xs text-muted-foreground">
                    <span>P95：{final.p95LatencyMs.toFixed(0)} ms</span>
                    <span>逾時率：{(final.timeoutRate * 100).toFixed(2)}%</span>
                  </div>
                </CardContent>
              </Card>
            </TabsContent>
          )
        })}
      </Tabs>

      <TraceTimeline rows={rows} />
    </div>
  )
}
