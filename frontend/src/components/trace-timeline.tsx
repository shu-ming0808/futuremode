import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { STRATEGIES, STRATEGY_LABEL } from "@/types"
import type { StrategyId } from "@/types"

const STRATEGY_COLOR: Record<StrategyId, string> = {
  fixed_capacity: "#f97316",
  reactive_autoscaling: "#eab308",
  predictive_prewarm: "#16a34a",
}

export type TimelineRow = {
  time: number
  arrivals: number
} & Record<
  | `queue_${StrategyId}`
  | `p95_${StrategyId}`
  | `accepted_${StrategyId}`
  | `timeout_${StrategyId}`
  | `pods_${StrategyId}`
  | `busy_pods_${StrategyId}`,
  number
>

export function TraceTimeline({ rows }: { rows: TimelineRow[] }) {
  const maxTime = rows[rows.length - 1]?.time ?? 0
  const timeDomain: [number, number] = [0, maxTime]

  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">請求到達量（每 tick，三策略共用同一流量）</CardTitle>
        </CardHeader>
        <CardContent style={{ height: 200 }}>
          <ResponsiveContainer>
            <LineChart data={rows}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis
                dataKey="time"
                type="number"
                domain={timeDomain}
                tick={{ fontSize: 10 }}
                label={{ value: "秒", position: "insideBottomRight", fontSize: 10, offset: -2 }}
              />
              <YAxis tick={{ fontSize: 10 }} />
              <Tooltip />
              <Line
                type="monotone"
                dataKey="arrivals"
                stroke="#64748b"
                dot={false}
                strokeWidth={1.5}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Queue 長度（三策略疊圖）</CardTitle>
        </CardHeader>
        <CardContent style={{ height: 200 }}>
          <ResponsiveContainer>
            <LineChart data={rows}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="time" type="number" domain={timeDomain} tick={{ fontSize: 10 }} />
              <YAxis tick={{ fontSize: 10 }} />
              <Tooltip />
              {STRATEGIES.map((s) => (
                <Line
                  key={s}
                  type="monotone"
                  dataKey={`queue_${s}`}
                  name={STRATEGY_LABEL[s]}
                  stroke={STRATEGY_COLOR[s]}
                  dot={false}
                  strokeWidth={1.5}
                  isAnimationActive={false}
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">P95 延遲（ms，三策略疊圖）</CardTitle>
        </CardHeader>
        <CardContent style={{ height: 200 }}>
          <ResponsiveContainer>
            <LineChart data={rows}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="time" type="number" domain={timeDomain} tick={{ fontSize: 10 }} />
              <YAxis tick={{ fontSize: 10 }} />
              <Tooltip />
              <ReferenceLine y={2000} stroke="#dc2626" strokeDasharray="4 4" />
              {STRATEGIES.map((s) => (
                <Line
                  key={s}
                  type="monotone"
                  dataKey={`p95_${s}`}
                  name={STRATEGY_LABEL[s]}
                  stroke={STRATEGY_COLOR[s]}
                  dot={false}
                  strokeWidth={1.5}
                  isAnimationActive={false}
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">累積完成量（三策略疊圖）</CardTitle>
        </CardHeader>
        <CardContent style={{ height: 200 }}>
          <ResponsiveContainer>
            <LineChart data={rows}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="time" type="number" domain={timeDomain} tick={{ fontSize: 10 }} />
              <YAxis tick={{ fontSize: 10 }} />
              <Tooltip />
              {STRATEGIES.map((s) => (
                <Line
                  key={s}
                  type="monotone"
                  dataKey={`accepted_${s}`}
                  name={STRATEGY_LABEL[s]}
                  stroke={STRATEGY_COLOR[s]}
                  dot={false}
                  strokeWidth={1.5}
                  isAnimationActive={false}
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">累積逾時量（三策略疊圖）</CardTitle>
        </CardHeader>
        <CardContent style={{ height: 200 }}>
          <ResponsiveContainer>
            <LineChart data={rows}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="time" type="number" domain={timeDomain} tick={{ fontSize: 10 }} />
              <YAxis tick={{ fontSize: 10 }} />
              <Tooltip />
              {STRATEGIES.map((s) => (
                <Line
                  key={s}
                  type="monotone"
                  dataKey={`timeout_${s}`}
                  name={STRATEGY_LABEL[s]}
                  stroke={STRATEGY_COLOR[s]}
                  dot={false}
                  strokeWidth={1.5}
                  isAnimationActive={false}
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Pod 數量（實線總數／虛線忙碌，三策略疊圖）</CardTitle>
        </CardHeader>
        <CardContent style={{ height: 200 }}>
          <ResponsiveContainer>
            <LineChart data={rows}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="time" type="number" domain={timeDomain} tick={{ fontSize: 10 }} />
              <YAxis tick={{ fontSize: 10 }} />
              <Tooltip />
              {STRATEGIES.map((s) => (
                <Line
                  key={`pods_${s}`}
                  type="stepAfter"
                  dataKey={`pods_${s}`}
                  name={`${STRATEGY_LABEL[s]}・總數`}
                  stroke={STRATEGY_COLOR[s]}
                  dot={false}
                  strokeWidth={1.5}
                  isAnimationActive={false}
                />
              ))}
              {STRATEGIES.map((s) => (
                <Line
                  key={`busy_pods_${s}`}
                  type="monotone"
                  dataKey={`busy_pods_${s}`}
                  name={`${STRATEGY_LABEL[s]}・忙碌`}
                  stroke={STRATEGY_COLOR[s]}
                  strokeDasharray="3 3"
                  dot={false}
                  strokeWidth={1}
                  isAnimationActive={false}
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </CardContent>
      </Card>
    </div>
  )
}
