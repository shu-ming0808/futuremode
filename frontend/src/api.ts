import type { ArrivalTraceResponse, Recommendation, ScenarioId } from "@/types"
import { buildMockArrivalTrace } from "@/mock-trace"
import { buildMockAssessment } from "@/mock-assessment"

// Demo 版本純前端硬編碼，不打後端：數字取自後端 compare_strategies 的實測 Monte Carlo 結果
// （見 mock-assessment.ts 註解），故意不接 /api/recommendation、/api/arrival-trace。
export async function fetchRecommendation(scenario: ScenarioId): Promise<Recommendation> {
  return buildMockAssessment(scenario)
}

export async function fetchArrivalTrace(scenario: ScenarioId): Promise<ArrivalTraceResponse> {
  return buildMockArrivalTrace(scenario)
}

