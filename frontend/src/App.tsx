import { useMemo, useState } from "react"
import { fetchArrivalTrace, fetchRecommendation } from "@/api"
import { ScenarioPicker } from "@/components/scenario-picker"
import { RiskCards } from "@/components/risk-cards"
import { RecommendationCard } from "@/components/recommendation-card"
import { StrategySimulation } from "@/components/strategy-simulation"
import type { ArrivalTraceResponse, Recommendation, ScenarioId } from "@/types"

export function App() {
  const [scenario, setScenario] = useState<ScenarioId | null>(null)
  const [arrivalTrace, setArrivalTrace] = useState<ArrivalTraceResponse | null>(null)
  const [assessment, setAssessment] = useState<Recommendation | null>(null)
  const [loadingAssessment, setLoadingAssessment] = useState(false)
  const [showSimulation, setShowSimulation] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSelectScenario(id: ScenarioId) {
    setScenario(id)
    setArrivalTrace(null)
    setAssessment(null)
    setShowSimulation(false)
    setError(null)
    setLoadingAssessment(true)
    try {
      const [arrivalResult, assessmentResult] = await Promise.all([
        fetchArrivalTrace(id),
        fetchRecommendation(id),
      ])
      setArrivalTrace(arrivalResult)
      setAssessment(assessmentResult)
    } catch (e) {
      setError(e instanceof Error ? e.message : "評估失敗")
    } finally {
      setLoadingAssessment(false)
    }
  }

  // 三策略初始 worker 數／節點延遲參數皆來自 /api/recommendation（風險調整後），
  // 前端只用 SingleRunEngine 逐 tick 自跑 queue/pod 狀態機，不再打 /api/single-run-trace。
  const trace = useMemo(() => {
    if (!arrivalTrace || !assessment) return null
    return {
      label: arrivalTrace.label,
      dt_seconds: assessment.dt_seconds,
      arrivals: arrivalTrace.arrivals,
      strategies: assessment.strategies,
      node_params: assessment.node_params,
    }
  }, [arrivalTrace, assessment])

  function handleRunSimulation() {
    setShowSimulation(true)
  }

  return (
    <div className="mx-auto max-w-6xl space-y-6 p-6">
      <header className="space-y-1">
        <h1 className="text-xl font-semibold">OpeningGuard AI — 開盤容量決策 Demo</h1>
        <p className="text-sm text-muted-foreground">
          選擇一個開盤情境，觀察三種容量策略（固定容量／反應式擴容／預測式預熱）的表現差異。
          所有數字為 Synthetic Demo Assumption，不代表券商真實容量。
        </p>
      </header>

      <section className="space-y-2">
        <h2 className="text-sm font-medium text-muted-foreground">1. 選擇開盤情境</h2>
        <ScenarioPicker
          selected={scenario}
          loading={loadingAssessment}
          onSelect={handleSelectScenario}
        />
      </section>

      {error && <p className="text-sm text-destructive">{error}</p>}

      {assessment && (
        <>
          <section className="space-y-2">
            <h2 className="text-sm font-medium text-muted-foreground">2. Agent 風險判斷</h2>
            <RiskCards risks={assessment.risks} />
          </section>

          <section className="space-y-2">
            <h2 className="text-sm font-medium text-muted-foreground">3. 建議容量方案</h2>
            <RecommendationCard
              recommended={assessment.recommended}
              warningCode={assessment.warning_code}
              onRunSimulation={handleRunSimulation}
            />
          </section>

          {showSimulation && trace && (
            <section className="space-y-2">
              <h2 className="text-sm font-medium text-muted-foreground">
                4. 單次模擬：三策略即時系統壓力
              </h2>
              <StrategySimulation key={`${scenario}-${trace.label}-${trace.strategies.predictive_prewarm.target_workers}`} data={trace} />
            </section>
          )}
        </>
      )}
    </div>
  )
}

export default App
