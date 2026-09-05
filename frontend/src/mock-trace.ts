import type { ArrivalTraceResponse, ScenarioId } from "@/types"
import { sampleLogNormal, sampleNormal, samplePoisson } from "@/lib/simulation/distributions"

const DT = 0.1
const DURATION_SECONDS = 300
const TICKS = DURATION_SECONDS / DT

// 直接對應 backend/openingguard/data/scenarios/*.json 的 traffic 區塊，
// 讓前端 mock 的到達曲線與後端 Monte Carlo 跑的是同一個流量模型。
const TRAFFIC: Record<
  ScenarioId,
  {
    baseline_rps: number
    scenario_multiplier: number
    open_spike_ratio: number
    decay_seconds: number
    intensity_sigma: number
    burst_sigma: number
  }
> = {
  normal: {
    baseline_rps: 190,
    scenario_multiplier: 1.0,
    open_spike_ratio: 1.55,
    decay_seconds: 75,
    intensity_sigma: 0.08,
    burst_sigma: 0.1,
  },
  high_pressure: {
    baseline_rps: 260,
    scenario_multiplier: 1.6,
    open_spike_ratio: 1.55,
    decay_seconds: 75,
    intensity_sigma: 0.13,
    burst_sigma: 0.18,
  },
  downstream_bottleneck: {
    baseline_rps: 250,
    scenario_multiplier: 2.4,
    open_spike_ratio: 1.6,
    decay_seconds: 90,
    intensity_sigma: 0.14,
    burst_sigma: 0.2,
  },
}

const SPIKE_FLOOR = 0.58 // 同 core.py::generate_arrival_trace
const BURST_AR_COEFFICIENT = 0.88

/** 同 core.py::generate_arrival_trace：衰減開盤尖峰 × 當日強度 shock × AR(1) 突發 → Poisson。 */
function buildArrivals(scenario: ScenarioId): number[] {
  const traffic = TRAFFIC[scenario]
  const dayShock = sampleLogNormal(-0.5 * traffic.intensity_sigma ** 2, traffic.intensity_sigma)

  const smooth: number[] = new Array(TICKS)
  smooth[0] = sampleNormal(0, traffic.burst_sigma)
  for (let i = 1; i < TICKS; i += 1) {
    smooth[i] =
      BURST_AR_COEFFICIENT * smooth[i - 1] +
      Math.sqrt(1 - BURST_AR_COEFFICIENT ** 2) * sampleNormal(0, traffic.burst_sigma)
  }

  return Array.from({ length: TICKS }, (_, tick) => {
    const seconds = tick * DT
    const spike =
      SPIKE_FLOOR +
      (traffic.open_spike_ratio - SPIKE_FLOOR) * Math.exp(-seconds / traffic.decay_seconds)
    const burst = Math.exp(smooth[tick] - 0.5 * traffic.burst_sigma ** 2)
    const rate = traffic.baseline_rps * traffic.scenario_multiplier * spike * dayShock * burst
    return samplePoisson(Math.max(rate * DT, 0))
  })
}

export function buildMockArrivalTrace(scenario: ScenarioId): ArrivalTraceResponse {
  return {
    label: scenario,
    dt_seconds: DT,
    duration_seconds: DURATION_SECONDS,
    arrivals: buildArrivals(scenario),
  }
}
