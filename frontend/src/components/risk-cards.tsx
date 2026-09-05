import { useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import type { RiskCard, Severity } from "@/types"

const SEVERITY_VARIANT: Record<Severity, string> = {
  low: "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-300",
  medium: "bg-yellow-100 text-yellow-800 dark:bg-yellow-950 dark:text-yellow-300",
  high: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300",
  uncertain: "bg-muted text-muted-foreground",
}

const SEVERITY_LABEL: Record<Severity, string> = {
  low: "低",
  medium: "中",
  high: "高",
  uncertain: "不確定",
}

function RiskCardItem({ risk }: { risk: RiskCard }) {
  const [expanded, setExpanded] = useState(false)
  return (
    <Card className="cursor-pointer" onClick={() => setExpanded((v) => !v)}>
      <CardHeader className="flex flex-row items-center justify-between gap-2">
        <CardTitle className="text-sm font-medium">{risk.title}</CardTitle>
        <Badge className={SEVERITY_VARIANT[risk.severity]}>
          {SEVERITY_LABEL[risk.severity]}
        </Badge>
      </CardHeader>
      {expanded && (
        <CardContent className="space-y-2 text-xs text-muted-foreground">
          <p>
            <span className="font-medium text-foreground">引用原文：</span>
            {risk.matched_input_text || "（無）"}
          </p>
          <p>
            <span className="font-medium text-foreground">來源：</span>
            <a
              href={risk.source_url}
              target="_blank"
              rel="noreferrer"
              className="underline"
              onClick={(e) => e.stopPropagation()}
            >
              {risk.source_url}
            </a>
          </p>
          <div>
            <span className="font-medium text-foreground">套用倍率：</span>
            <ul className="mt-1 list-inside list-disc">
              {Object.entries(risk.effects)
                .filter(([, v]) => v !== null)
                .map(([key, value]) => (
                  <li key={key}>
                    {key}: {value}
                  </li>
                ))}
            </ul>
          </div>
        </CardContent>
      )}
    </Card>
  )
}

export function RiskCards({ risks }: { risks: RiskCard[] }) {
  return (
    <div className="space-y-3">
      {risks.length === 0 ? (
        <p className="text-sm text-muted-foreground">此情境下未匹配到風險目錄項目。</p>
      ) : (
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
          {risks.map((risk) => (
            <RiskCardItem key={risk.risk_id} risk={risk} />
          ))}
        </div>
      )}
    </div>
  )
}
