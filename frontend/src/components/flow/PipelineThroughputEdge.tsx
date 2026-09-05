import { memo } from "react"
import { BaseEdge, getBezierPath, type EdgeProps } from "@xyflow/react"

export interface PipelineThroughputEdgeData {
  throughputRatio: number // 0~1，該邊當前流量壓力（用於決定線寬）
  congested: boolean // 目標節點是否壅塞（用於染色，直觀看到「流量正湧向瓶頸」）
  [key: string]: unknown
}

function widthForRatio(ratio: number): number {
  return 1.5 + Math.min(1, Math.max(0, ratio)) * 4.5
}

function PipelineThroughputEdgeImpl({
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  data,
  markerEnd,
}: EdgeProps) {
  const { throughputRatio = 0, congested = false } =
    (data as unknown as PipelineThroughputEdgeData) ?? {}

  const [edgePath] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  })

  const strokeWidth = widthForRatio(throughputRatio)
  const strokeColor = congested ? "#ef4444" : throughputRatio > 0 ? "#38bdf8" : "#525252"

  return <BaseEdge path={edgePath} markerEnd={markerEnd} style={{ stroke: strokeColor, strokeWidth }} />
}

export const PipelineThroughputEdge = memo(PipelineThroughputEdgeImpl)
