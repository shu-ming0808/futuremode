import type { ScenarioId } from "@/types"

export const SCENARIO_PRESETS: {
  id: ScenarioId
  label: string
  description: string
}[] = [
  {
    id: "normal",
    label: "一般開盤",
    description: "常態流量，無特殊營運事件。",
  },
  {
    id: "high_pressure",
    label: "高壓開盤",
    description: "夜盤消息面偏多，預期開盤量能偏高。",
  },
  {
    id: "downstream_bottleneck",
    label: "下游瓶頸",
    description: "DB／交易閘道容量吃緊的情境，凸顯硬上限限制。",
  },
]
