# OpeningGuard AI 後端

這是券商內部容量決策 Demo 的後端第一版。它以合成開盤下單流量比較固定容量、反應式擴容與預測式預熱，並把 worker、Database 與交易閘道容量都納入限制。

所有結果都是 Synthetic Demo Assumption，不代表任何券商真實容量，也不會連接或修改真實交易系統。Agent 的決策固定停在 `pending_human_approval`。

## 使用 uv 啟動

```powershell
cd backend
uv sync
uv run fastapi dev main.py
```

API：

- `GET /api/health`
- `POST /api/simulate`

請求範例：

```json
{
  "scenario": "high_pressure",
  "runs": 30,
  "operation_note": "今晚部署新版下單服務，夜盤量能偏高，明早可能大量送單",
  "use_agent": false,
  "seed": 20260904
}
```

CLI：

```powershell
uv run openingguard --scenario high_pressure --runs 30
uv run openingguard --scenario downstream_bottleneck --runs 10 --json
```

## OpenAI Agent 模式

將 API key 放在環境變數，不要寫入 Git：

```powershell
$env:OPENAI_API_KEY="your-key"
$env:OPENAI_MODEL="gpt-5.1"
uv run openingguard --scenario high_pressure --runs 30 --agent
```

Agent 會從固定風險目錄選擇 `risk_id`，再透過 OpenAI Responses API 的 function calling 呼叫確定性容量工具。沒有 API key 時，輸出會清楚標示 `offline_fallback` 與 `is_llm_result: false`，不可當成 LLM 成果。

## Demo 情境與資料

- `normal`：正常開盤。
- `high_pressure`：市場壓力加版本更新；展示提前預熱。
- `downstream_bottleneck`：DB／交易閘道成為硬上限；展示增加 worker 也無法解決。
- `openingguard/data/risk_catalog.json`：10 種預先定義風險與固定模擬倍率。
- `openingguard/data/eval_cases.json`：12 筆人工標記 Agent 評測案例。

目前使用 100 ms 批次 FIFO 模型；客戶端逾時不取消後端工作，重試沿用同一概念 UUID 並排到隊尾。這不是正式防重複交易保證，真實系統仍需持久化 idempotency key 與交易狀態。

## 統計圖表 Notebook

Notebook 內每個程式區塊前都有簡短說明，並包含合成流量、尖峰分布、Monte Carlo 信賴區間、策略成本、參數掃描與下游硬上限圖表：

```powershell
uv sync --dev
uv run jupyter lab notebooks/statistical_analysis.ipynb
```
