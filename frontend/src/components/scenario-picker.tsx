import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { SCENARIO_PRESETS } from "@/scenarios"
import type { ScenarioId } from "@/types"

export function ScenarioPicker({
  selected,
  loading,
  onSelect,
}: {
  selected: ScenarioId | null
  loading: boolean
  onSelect: (id: ScenarioId) => void
}) {
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
      {SCENARIO_PRESETS.map((preset) => (
        <Card
          key={preset.id}
          className={
            selected === preset.id ? "border-primary ring-1 ring-primary" : ""
          }
        >
          <CardHeader>
            <CardTitle className="text-base">{preset.label}</CardTitle>
            <CardDescription>{preset.description}</CardDescription>
          </CardHeader>
          <CardContent>
            <Button
              className="w-full"
              disabled={loading}
              variant={selected === preset.id ? "default" : "outline"}
              onClick={() => onSelect(preset.id)}
            >
              {loading && selected === preset.id ? "評估中…" : "執行此情境"}
            </Button>
          </CardContent>
        </Card>
      ))}
    </div>
  )
}
