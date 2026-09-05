import { memo } from "react"
import { Handle, Position, type NodeProps } from "@xyflow/react"
import { AlertTriangle } from "lucide-react"
import { cn } from "@/lib/utils"

const UTIL_HIGH = 0.85 // 超過此利用率視為高負載
const UTIL_MID = 0.6

export interface PipelineQueueNodeData {
  label: string
  sublabel?: string
  kind: "queue" | "pods" | "utilization" | "terminal"
  hasTarget: boolean
  hasSource: boolean
  // kind === "queue"
  queueLength?: number
  queueCapacity?: number
  // kind === "pods"
  busyPods?: number
  totalPods?: number
  workerUtilizations?: number[]
  utilization?: number
  // kind === "utilization"
  utilizationLabel?: string
  // kind === "terminal"
  count?: number
  isAlert?: boolean
  [key: string]: unknown
}

function utilColor(util: number): string {
  if (util >= UTIL_HIGH) return "bg-red-500"
  if (util >= UTIL_MID) return "bg-amber-500"
  if (util > 0) return "bg-emerald-500"
  return "bg-neutral-700"
}

function PipelineQueueNodeImpl({ data }: NodeProps) {
  const {
    label,
    sublabel,
    kind,
    hasTarget,
    hasSource,
    queueLength = 0,
    queueCapacity = 1,
    busyPods = 0,
    totalPods = 0,
    workerUtilizations = [],
    utilization = 0,
    utilizationLabel,
    count = 0,
    isAlert = false,
  } = data as unknown as PipelineQueueNodeData

  const queueRatio = kind === "queue" ? Math.min(1, queueLength / Math.max(1, queueCapacity)) : 0
  const podUtilization = totalPods > 0 ? busyPods / totalPods : 0
  // 一格一台 worker，格數隨擴縮容增減，與標題的「N 台」永遠一致；
  // 每格的顏色深淺是該台自己的 slot 佔用率，而非整體平均。
  const isBottleneck =
    (kind === "pods" && podUtilization >= UTIL_HIGH && busyPods > 0) ||
    (kind === "queue" && queueRatio >= UTIL_HIGH) ||
    (kind === "utilization" && utilization >= UTIL_HIGH)

  return (
    <div
      className={cn(
        "w-44 rounded-lg border bg-neutral-900 text-neutral-100 shadow-md transition-colors",
        isBottleneck || isAlert ? "border-red-500/70" : "border-neutral-700"
      )}
    >
      {hasTarget && (
        <Handle type="target" position={Position.Left} className="!bg-neutral-500 !w-2 !h-2" />
      )}
      {hasSource && (
        <Handle type="source" position={Position.Right} className="!bg-neutral-500 !w-2 !h-2" />
      )}

      <div className="flex items-center justify-between px-3 py-2 border-b border-neutral-800">
        <div className="min-w-0">
          <div className="text-sm font-medium truncate">{label}</div>
          {sublabel && (
            <div className="text-[10px] text-neutral-500 uppercase tracking-wide">{sublabel}</div>
          )}
        </div>
        {(isBottleneck || isAlert) && <AlertTriangle className="w-4 h-4 text-red-400 shrink-0" />}
      </div>

      <div className="px-3 py-2 space-y-2">
        {kind === "pods" && (
          <div>
            <div className="flex items-center justify-between text-[10px] text-neutral-500 mb-1">
              <span>SLOTS</span>
              <span className="font-mono">
                {busyPods}/{totalPods}
              </span>
            </div>
            <div className="flex flex-wrap gap-1">
              {workerUtilizations.map((workerUtil, i) => (
                <div
                  key={i}
                  className={cn(
                    "w-3.5 h-3.5 rounded-sm",
                    workerUtil > 0 ? utilColor(workerUtil) : "bg-neutral-800",
                    workerUtil >= UTIL_HIGH && "animate-pulse"
                  )}
                  style={workerUtil > 0 ? { opacity: 0.35 + 0.65 * workerUtil } : undefined}
                  title={`worker #${i + 1}：${(workerUtil * 100).toFixed(0)}% 佔用`}
                />
              ))}
            </div>
            <div className="text-[10px] text-neutral-500 font-mono">
              {workerUtilizations.length} 台・{(podUtilization * 100).toFixed(0)}% 佔用
            </div>
          </div>
        )}

        {kind === "queue" && (
          <div>
            <div className="flex items-center justify-between text-[10px] text-neutral-500 mb-1">
              <span>QUEUE</span>
              <span className="font-mono">
                {queueLength}/{queueCapacity}
              </span>
            </div>
            <div className="h-1.5 rounded-full bg-neutral-800 overflow-hidden">
              <div
                className={cn(
                  "h-full rounded-full transition-[width]",
                  queueRatio >= 0.8 ? "bg-red-500" : queueRatio >= 0.4 ? "bg-amber-500" : "bg-sky-500"
                )}
                style={{ width: `${queueRatio * 100}%` }}
              />
            </div>
          </div>
        )}

        {kind === "utilization" && (
          <div>
            <div className="flex items-center justify-between text-[10px] text-neutral-500 mb-1">
              <span>{utilizationLabel ?? "UTIL"}</span>
              <span className="font-mono">{(utilization * 100).toFixed(0)}%</span>
            </div>
            <div className="h-1.5 rounded-full bg-neutral-800 overflow-hidden">
              <div
                className={cn("h-full rounded-full transition-[width]", utilColor(utilization))}
                style={{ width: `${Math.min(1, utilization) * 100}%` }}
              />
            </div>
          </div>
        )}

        {kind === "terminal" && (
          <div className="flex items-center justify-between pt-0.5">
            <span className="text-[10px] text-neutral-500">累積</span>
            <span
              className={cn(
                "font-mono text-lg font-semibold",
                isAlert ? "text-red-400" : "text-neutral-100"
              )}
            >
              {count}
            </span>
          </div>
        )}
      </div>
    </div>
  )
}

export const PipelineQueueNode = memo(PipelineQueueNodeImpl)
