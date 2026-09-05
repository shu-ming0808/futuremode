import { useMemo } from "react"
import { ReactFlow, Background, BackgroundVariant, type Edge, type Node } from "@xyflow/react"
import "@xyflow/react/dist/style.css"
import type { NodeMetrics } from "@/lib/simulation/types"
import { PipelineQueueNode, type PipelineQueueNodeData } from "@/components/flow/PipelineQueueNode"
import {
  PipelineThroughputEdge,
  type PipelineThroughputEdgeData,
} from "@/components/flow/PipelineThroughputEdge"

const nodeTypes = { pipelineQueueNode: PipelineQueueNode }
const edgeTypes = { pipelineThroughputEdge: PipelineThroughputEdge }

const UTIL_HIGH = 0.85

type PipelineFlowProps = {
  metrics: NodeMetrics | null
  acceptedCumulative: number
  timeoutCumulative: number
}

const NODE_POSITIONS = {
  queue: { x: 0, y: 130 },
  worker: { x: 220, y: 130 },
  db: { x: 460, y: 30 },
  gateway: { x: 460, y: 230 },
  accepted: { x: 680, y: 30 },
  timeout: { x: 680, y: 230 },
}

export function PipelineFlow({ metrics, acceptedCumulative, timeoutCumulative }: PipelineFlowProps) {
  const dbRatio = metrics?.dbUtilization ?? 0
  const gatewayRatio = metrics?.gatewayUtilization ?? 0
  const queueCapacity = Math.max(1, metrics?.queueCapacity ?? 1)
  const queueRatio = metrics ? Math.min(1, metrics.queueLength / queueCapacity) : 0
  const isTimeout = timeoutCumulative > 0

  const nodes: Node[] = useMemo(() => {
    const build = (id: string, position: { x: number; y: number }, data: PipelineQueueNodeData): Node => ({
      id,
      type: "pipelineQueueNode",
      position,
      data: data as unknown as Record<string, unknown>,
      draggable: false,
      selectable: false,
    })

    return [
      build("queue", NODE_POSITIONS.queue, {
        label: "Queue",
        kind: "queue",
        hasTarget: false,
        hasSource: true,
        queueLength: metrics?.queueLength ?? 0,
        queueCapacity,
      }),
      build("worker", NODE_POSITIONS.worker, {
        label: `Worker（${metrics?.currentWorkers ?? 0} 台）`,
        kind: "pods",
        hasTarget: true,
        hasSource: true,
        busyPods: metrics?.busyPods ?? 0,
        totalPods: metrics?.totalPods ?? 0,
        workerUtilizations: metrics?.workerUtilizations ?? [],
      }),
      build("db", NODE_POSITIONS.db, {
        label: "Database",
        kind: "utilization",
        hasTarget: true,
        hasSource: true,
        utilization: dbRatio,
        utilizationLabel: "UTIL",
      }),
      build("gateway", NODE_POSITIONS.gateway, {
        label: "Gateway",
        kind: "utilization",
        hasTarget: true,
        hasSource: true,
        utilization: gatewayRatio,
        utilizationLabel: "UTIL",
      }),
      build("accepted", NODE_POSITIONS.accepted, {
        label: "Accepted",
        kind: "terminal",
        hasTarget: true,
        hasSource: false,
        count: acceptedCumulative,
      }),
      build("timeout", NODE_POSITIONS.timeout, {
        label: "Timeout",
        kind: "terminal",
        hasTarget: true,
        hasSource: false,
        count: timeoutCumulative,
        isAlert: isTimeout,
      }),
    ]
  }, [metrics, acceptedCumulative, timeoutCumulative, dbRatio, gatewayRatio, isTimeout, queueCapacity])

  const edges: Edge[] = useMemo(() => {
    const build = (
      id: string,
      source: string,
      target: string,
      throughputRatio: number,
      congested: boolean
    ): Edge => ({
      id,
      source,
      target,
      type: "pipelineThroughputEdge",
      data: { throughputRatio, congested } satisfies PipelineThroughputEdgeData as unknown as Record<
        string,
        unknown
      >,
      markerEnd: { type: "arrowclosed", color: congested ? "#ef4444" : "#525252" },
    })

    const dbCongested = dbRatio >= UTIL_HIGH
    const gatewayCongested = gatewayRatio >= UTIL_HIGH

    return [
      build("e-queue-worker", "queue", "worker", queueRatio, queueRatio >= UTIL_HIGH),
      build("e-worker-db", "worker", "db", dbRatio, dbCongested),
      build("e-worker-gateway", "worker", "gateway", gatewayRatio, gatewayCongested),
      build("e-db-accepted", "db", "accepted", dbRatio, dbCongested),
      build("e-gateway-accepted", "gateway", "accepted", gatewayRatio, gatewayCongested),
      build("e-queue-timeout", "queue", "timeout", isTimeout ? 1 : 0, isTimeout),
    ]
  }, [dbRatio, gatewayRatio, queueRatio, isTimeout])

  return (
    <div className="h-[32rem] w-full rounded-md border bg-neutral-950">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        panOnDrag={false}
        zoomOnScroll={false}
        fitView
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} color="#333" gap={20} />
      </ReactFlow>
    </div>
  )
}
