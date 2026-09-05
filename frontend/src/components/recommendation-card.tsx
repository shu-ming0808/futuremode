import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import frontendCopy from "@/frontend_copy.json"
import type { CapacityRecommendation } from "@/types"

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="text-lg font-semibold">{value}</div>
    </div>
  )
}

export function RecommendationCard({
  recommended,
  warningCode,
  onRunSimulation,
}: {
  recommended: CapacityRecommendation | null
  warningCode: "no_feasible_plan" | null
  onRunSimulation: () => void
}) {
  if (!recommended || warningCode === "no_feasible_plan") {
    return (
      <Alert variant="destructive">
        <AlertTitle>無安全預熱方案</AlertTitle>
        <AlertDescription>
          {frontendCopy.warning_code.no_feasible_plan}
        </AlertDescription>
      </Alert>
    )
  }

  return (
    <Card className="border-primary">
      <CardHeader>
        <CardTitle>建議方案：預測式預熱 {recommended.workers} 台 worker</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex flex-wrap gap-6">
          <Stat
            label="預期 P95 延遲"
            value={`${recommended.p95_latency_ms.toFixed(0)} ms`}
          />
          <Stat
            label="壅塞機率（95% CI 上界）"
            value={`${(recommended.congestion_probability_ci95[1] * 100).toFixed(1)}%`}
          />
          <Stat
            label="逾時率"
            value={`${(recommended.timeout_rate * 100).toFixed(2)}%`}
          />
          <Stat
            label="Worker 成本"
            value={`${recommended.cost.toFixed(0)} worker-min`}
          />
        </div>
        <Button onClick={onRunSimulation}>執行單次模擬驗證</Button>
      </CardContent>
    </Card>
  )
}
