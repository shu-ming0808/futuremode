# OpeningGuard AI 後端

## 專案目的

OpeningGuard AI 是券商內部使用的開盤容量決策 Demo。系統以合成下單請求建立 Monte Carlo 排隊模擬，比較固定容量、反應式 autoscaling 與預測式預熱，並由 LLM Agent 從固定風險目錄選擇相關風險後呼叫確定性的容量評估工具。

本專案回答兩個問題：

1. 開盤尖峰發生時，提前預熱是否能比反應式擴容更早控制 Queue 與延遲？
2. 當 Database 或交易閘道已成為硬上限時，為什麼繼續增加 worker 仍無法解決壅塞？

所有 RPS、容量與成本都是 **Synthetic Demo Assumption**，不代表任何券商真實數據。系統不連接真實下單服務，也不自動修改基礎設施；建議固定停在 `pending_human_approval`。

## 系統流程圖

```mermaid
flowchart TD
    A["結構化情境參數"] --> D["容量評估工具"]
    B["自然語言營運備註"] --> C["OpenAI Agent<br/>選擇既有 risk_id"]
    C --> D
    E["固定風險目錄<br/>與預先定義倍率"] --> C
    E --> D

    D --> F["合成下單到達率"]
    F --> G["單一 FIFO Queue"]
    G --> H["API Workers"]
    H --> I["Database 硬上限"]
    I --> J["交易閘道硬上限"]

    D --> K["固定容量"]
    D --> L["反應式 Autoscaling"]
    D --> M["預測式預熱"]
    K --> N["Monte Carlo 指標與 95% CI"]
    L --> N
    M --> N
    N --> O["最低成本安全方案<br/>或無解警告"]
    O --> P["等待工程師人工核准"]
```

## 技術架構

| 項目 | 現行設定 | 用途 |
|---|---|---|
| Python | `>=3.13` | 模擬器、API 與 Agent |
| 套件管理 | `uv`、`pyproject.toml`、`uv.lock` | 建立可重現環境與鎖定依賴 |
| API | FastAPI | 提供 health check 與容量模擬端點 |
| 數值計算 | NumPy | 合成流量與 Monte Carlo 統計 |
| Agent | OpenAI Responses API function calling | 從固定目錄選風險並呼叫容量工具 |
| Schema | Pydantic | 驗證 API 輸入 |
| 統計分析 | pandas、Matplotlib、Seaborn、JupyterLab | 產生表格、信賴區間與敏感度圖 |
| 儲存 | JSON 與記憶體 | MVP 情境、風險目錄及執行結果；目前無正式 Database |

## 現行模擬模型

### 1. 時間與 Queue

- 模擬期間為開盤後 300 秒。
- 時間步長為 $\Delta t=0.1$ 秒。
- 相同 100 ms 內的請求以 cohort 彙整，進入單一 FIFO Queue。
- Queue 滿時，超出容量的 attempts 記為失敗。
- 客戶端逾時不會取消後端原請求；符合重試規則時，新 attempt 會排到 Queue 尾端。
- 重試沿用同一個概念 idempotency key，因此模擬會記錄被防止的重複副作用；目前不是逐 UUID 的持久化實作。

這是批次化排隊近似，不是逐筆 SimPy 或正式交易系統。

### 2. 合成到達率

第 $t$ 個時間點的條件到達率為：

$$
\lambda_t = \lambda_0 \times m \times
\left[0.58+(r_{open}-0.58)e^{-t/\tau}\right]
\times D \times B_t
$$

其中：

| 符號 | 意義 |
|---|---|
| $\lambda_0$ | baseline RPS |
| $m$ | 情境流量倍率 `scenario_multiplier` |
| $r_{open}$ | 盤初尖峰倍率 `open_spike_ratio` |
| $\tau$ | 衰減時間 `decay_seconds` |
| $D$ | 每個模擬日共用的 lognormal 強度衝擊，平均值校正為 1 |
| $B_t$ | 由 AR(1) Gaussian shock 轉換的短期群聚倍率，相關係數固定為 0.88 |

每 100 ms 的到達量再抽樣為：

$$
N_t \sim \operatorname{Poisson}(\lambda_t\Delta t)
$$

因此目前可稱為「帶有日級與短期強度擾動的 mixed-Poisson 合成模型」，不能宣稱真實券商下單流量已被證明符合此分布。Hawkes process 僅列為取得逐筆事件時間後的未來配適候選。

### 3. 每個時間步的有效容量

每個 tick 的三項容量為：

$$
C_{worker}=\lfloor W\times c_w\times\Delta t\rfloor
$$

$$
C_{db}=\lfloor n_{conn}\times c_{conn}\times\Delta t\rfloor
$$

$$
C_{gateway}=\lfloor c_g\times\Delta t\rfloor
$$

實際可處理數量取三者最小值：

$$
C_{effective}=\min(C_{worker},C_{db},C_{gateway})
$$

這個最小值設計用來呈現「一直增加 worker 不是答案」：只要 DB 或交易閘道先達上限，worker 容量再高也不會提升端到端吞吐。

目前 `worker_capacity_rps` 是單機有效吞吐的簡化參數，尚未拆成 concurrency 與服務時間分布。

## 三種容量策略

| 策略 | worker 行為 | 目的 |
|---|---|---|
| `fixed_capacity` | 全程維持 `current_workers` | 比較基準 |
| `reactive_autoscaling` | Queue 達到 `queue_threshold` 後觸發，等待 `worker_warmup_seconds` 才切換至 `target_workers` | 呈現監控與冷啟動延遲 |
| `predictive_prewarm` | 模擬開始前直接配置 `target_workers` | 呈現提前預熱的價值 |

目前反應式策略是一次由 current 跳到 target，不包含逐批擴容、scale-down、CPU 指標或 Kubernetes HPA 的完整控制迴路。

## 壅塞判定與統計指標

任一條件成立就把該次模擬判為壅塞：

- P95 延遲 $\geq 2$ 秒。
- attempt 逾時率 $\geq 0.1\%$。
- Queue 長度達到 Queue 容量。
- Database 或交易閘道 peak utilization $\geq 95\%$。

| 指標 | 定義 |
|---|---|
| `congestion_probability` | Monte Carlo runs 中被判為壅塞的比例 |
| `congestion_probability_ci95` | 二項比例的 95% Wilson interval |
| `p95_latency_ms` | 各 run 原始請求延遲 P95 的中位數 |
| `timeout_rate` | 逾時 attempts／全部 attempts |
| `accepted_within_slo_rate` | 2 秒內接受的原始委託／原始委託 |
| `max_queue` | 各 run 最大 Queue 長度的第 95 百分位數 |
| `database_peak_utilization` | 各 run DB peak utilization 的平均 |
| `gateway_peak_utilization` | 各 run gateway peak utilization 的平均 |
| `retry_amplification_factor` | 全部 attempts／原始請求 |
| `total_worker_minutes` | 模擬期間 worker 數對時間的積分，作為相對成本 |

`relevance_score` 只表示 Agent 對風險的排序分數，不是風險發生機率；`congestion_probability` 才是模擬產生的機率估計。

## 三個固定 Demo 情境

### 流量參數

| 情境 | baseline RPS | 情境倍率 | 盤初倍率 | 衰減秒數 | 日強度 sigma | burst sigma |
|---|---:|---:|---:|---:|---:|---:|
| `normal` | 190 | 1.0 | 1.55 | 75 | 0.08 | 0.10 |
| `high_pressure` | 260 | 1.6 | 1.55 | 75 | 0.13 | 0.18 |
| `downstream_bottleneck` | 250 | 2.4 | 1.60 | 90 | 0.14 | 0.20 |

### 系統容量參數

| 情境 | current workers | target workers | 單機 RPS | warmup 秒 | Queue 門檻／容量 | DB connections × RPS | Gateway RPS |
|---|---:|---:|---:|---:|---:|---:|---:|
| `normal` | 6 | 10 | 80 | 30 | 300／12,000 | 20 × 60 = 1,200 | 1,200 |
| `high_pressure` | 6 | 14 | 80 | 30 | 300／12,000 | 20 × 60 = 1,200 | 1,200 |
| `downstream_bottleneck` | 6 | 16 | 80 | 30 | 250／12,000 | 12 × 45 = 540 | 560 |

### SLO 與重試參數

| 情境 | SLO | 最大逾時率 | 最大壅塞機率 | 下游安全利用率 | retry policy | max retries | delay |
|---|---:|---:|---:|---:|---|---:|---|
| `normal` | 2 秒 | 0.1% | 5% | 95% | backoff + jitter | 2 | base 0.25 秒，max 1 秒 |
| `high_pressure` | 2 秒 | 0.1% | 5% | 95% | backoff + jitter | 2 | base 0.25 秒，max 1 秒 |
| `downstream_bottleneck` | 2 秒 | 0.1% | 5% | 95% | immediate | 2 | 0.1 秒 |

三組情境都使用 candidate workers `[6, 8, 10, 12, 14, 16]`。目前情境版本為 `2026-09-04-v1`，設定檔由 Git 管理；每次結果也回傳 `scenario_version` 與 `simulator_version`，使報告能追溯使用哪一版參數。數值是可替換的 Demo 假設，應在取得壓測或營運資料後建立新版本，不直接覆寫舊結果的解讀依據。

## 容量方案搜尋

搜尋器目前只針對 `predictive_prewarm` 逐一測試 candidate workers，並依下列限制判斷方案是否可行：

```text
P95 latency < 2 seconds
timeout rate < 0.1%
congestion probability < 5%
database peak utilization < 95%
gateway peak utilization < 95%
```

可行方案依 worker-minutes、壅塞機率、P95 與 worker 數排序，選出最低成本方案。如果所有方案都失敗，回傳 DB／交易閘道、限流、降載或人工處理警告，不硬選不安全方案。

目前 `prewarm_at` 固定回傳 `08:50`；尚未把預熱時間、Queue threshold、concurrency 與 `maxReplicas` 放入聯合搜尋。

## OpenAI Agent 決策流程

1. 後端依 `scenario_id` 載入版本化的固定結構化參數，呼叫端只提供情境 ID 與自然語言營運備註。
2. 模型只能從 `risk_catalog.json` 選擇最多三個既有 `risk_id`。
3. `matched_input_text` 必須逐字出現在營運備註中。
4. 模型必須呼叫 `run_capacity_assessment` function tool。
5. Python 從可信風險目錄補上來源 URL，不接受模型自行產生來源。
6. Python 套用目錄中事先定義的 medium 倍率並執行確定性模擬。
7. 模型解釋工具結果，但不可提出工具未計算的 worker 數。
8. 最終狀態維持 `pending_human_approval`。

### Medium 風險倍率

| risk_id | 套用效果 |
|---|---|
| `market_volatility_order_spike` | arrival × 1.5 |
| `release_product_file_download` | worker capacity × 0.9 |
| `database_connection_saturation` | DB capacity × 0.7 |
| `gateway_rate_limit` | gateway capacity × 0.7 |
| `retry_storm` | max retries = 2 |
| `worker_capacity_saturation` | worker capacity × 0.75 |
| `cold_start_autoscaling_delay` | warmup time × 1.5 |
| `cache_cold_start` | DB capacity × 0.8 |
| `network_dependency_failure` | 目前只提示人工檢查，不套數值 |
| `unknown_unquantified_risk` | 不強行量化，交由人工確認 |

若沒有 `OPENAI_API_KEY`，程式會改用簡單關鍵字備援，並明確標示：

```json
{
  "mode": "offline_fallback",
  "is_llm_result": false
}
```

離線備援不能當成 LLM Demo 或 Agent 準確率證據。

## 快速開始

### 1. 建立 uv 環境

```powershell
cd backend
uv sync
```

### 2. 啟動 FastAPI

```powershell
uv run fastapi dev main.py
```

開發伺服器預設為 `http://127.0.0.1:8000`，互動文件為 `http://127.0.0.1:8000/docs`。

### 3. 執行單一 CLI 情境

```powershell
uv run openingguard --scenario high_pressure --runs 30
```

### 4. 執行下游瓶頸情境並輸出完整 JSON

```powershell
uv run openingguard --scenario downstream_bottleneck --runs 30 --json
```

### 5. 設定 OpenAI Agent

API key 只能放在環境變數，不可提交到 Git：

```powershell
$env:OPENAI_API_KEY="your-key"
$env:OPENAI_MODEL="gpt-5.1"
uv run openingguard --scenario high_pressure --runs 30 --agent
```

### 6. 執行 12 筆人工標記的 Agent 評測

```powershell
uv run openingguard --eval
```

沒有 API key 時，此命令會拒絕執行，避免把離線關鍵字備援誤報為 LLM 評測。

### 7. 開啟統計 Notebook

```powershell
uv sync --dev
uv run jupyter lab notebooks/statistical_analysis.ipynb
```

Notebook 已嵌入執行結果，包含合成流量、peak RPS 分布、Monte Carlo、Wilson interval、延遲、成本、參數掃描及下游硬上限圖。

## API

### `GET /api/health`

回傳版本、可用情境及是否存在 OpenAI API key；不會回傳 key 本身。

### `POST /api/simulate`

請求格式：

```json
{
  "scenario": "high_pressure",
  "runs": 30,
  "operation_note": "今晚部署新版下單服務，夜盤量能偏高，明早可能大量送單",
  "use_agent": true,
  "seed": 20260904
}
```

| 欄位 | 型別 | 限制 | 說明 |
|---|---|---|---|
| `scenario` | string | 必須是既有情境 ID | 預設 `normal` |
| `runs` | integer | 1～500 | Monte Carlo 次數，預設 30 |
| `operation_note` | string or null | 選填 | Agent 使用的營運備註；空值使用情境預設文字 |
| `use_agent` | boolean | — | 是否執行風險選擇流程 |
| `seed` | integer | — | 固定亂數種子 |

主要回傳欄位：

| 欄位 | 說明 |
|---|---|
| `run_id` | 本次執行 UUID |
| `scenario_version` | 本次使用的固定情境參數版本 |
| `simulator_version` | 本次使用的模擬器版本 |
| `synthetic_assumption` | 固定為 true，提醒數據是合成假設 |
| `risk_matches` | Agent 選出的風險與可信目錄來源 |
| `scenarios` | 三種策略的效能、風險與成本 |
| `recommended` | 最低成本安全預熱方案；無安全方案時為 null |
| `candidate_plans` | 所有 candidate worker 的評估結果 |
| `warning` | worker-only 無解時的限制說明 |
| `approval_status` | 固定為 `pending_human_approval` |
| `agent` | Agent 模式、模型、tool call 與是否為真正 LLM 結果 |

目前 API 刻意不接受呼叫端自行傳入 RPS、倍率、P50/P90/P99、market features 或 confidence。呼叫端只能選 `scenario_id`；Agent 只能選擇既有 `risk_id`，不能創造情境數值。若未來開放自訂參數，必須使用另一個受嚴格驗證的管理流程，不交由 LLM 直接填值。

## 統計 Notebook 流程

`notebooks/statistical_analysis.ipynb` 每個 code cell 前都有 Markdown 說明，流程如下：

1. 固定 scenario、runs 與 random seed。
2. 顯示完整情境參數。
3. 繪製單一合成開盤流量路徑。
4. 以 100 個合成日呈現 peak RPS 分布。
5. 比較三種策略的 Monte Carlo 結果。
6. 顯示壅塞機率與 95% Wilson interval。
7. 對照 P95 延遲與 2 秒 SLO。
8. 比較 worker-minutes 與 SLO 內接受率。
9. 掃描相對流量 × 預熱 worker 數。
10. 以熱圖檢查結論是否只對單一參數成立。
11. 顯示 Database／交易閘道造成的容量平台。
12. 輸出可引用的統計摘要表與限制。

## 專案結構

```text
backend/
│
├── README.md                         # 技術、參數與使用說明
├── pyproject.toml                    # uv 專案與依賴設定
├── uv.lock                           # 鎖定後的完整依賴版本
├── .env.example                      # OpenAI 環境變數範例
├── main.py                           # FastAPI ASGI 入口
├── schemas.py                        # 舊入口的相容匯入
│
├── notebooks/
│   └── statistical_analysis.ipynb    # 統計分析、圖表與敏感度掃描
│
└── openingguard/
    ├── __init__.py                   # 套件版本
    ├── api.py                        # FastAPI endpoints
    ├── schemas.py                    # Pydantic request schema
    ├── cli.py                        # CLI 與互動式 Demo
    ├── agent.py                      # Responses API tool calling 與評測
    ├── core.py                       # 流量、Queue、策略與容量搜尋核心
    └── data/
        ├── risk_catalog.json         # 10 種固定風險與倍率
        ├── eval_cases.json           # 12 筆人工標記 Agent 案例
        └── scenarios/
            ├── normal.json
            ├── high_pressure.json
            └── downstream_bottleneck.json
```

## 現行驗證

- 相同輸入與 seed 可重現相同統計結果；`run_id` 與建立時間除外。
- 三個 API 情境均可正常回傳。
- normal 與 high-pressure 可產生 candidate recommendation。
- downstream-bottleneck 會拒絕 worker-only 方案並回傳警告。
- 沒有 API key 時，Agent 結果正確標示為非 LLM。
- Notebook 12 個 code cells 已完整執行，內嵌 7 張圖，沒有 cell error。

## 未完成與已知限制

- [ ] 若未來需要自訂情境，另建受權限與 schema 保護的管理流程；現行公開模擬 API 維持只接受版本化 `scenario_id`。
- [ ] 將容量搜尋擴充到 `maxReplicas`、concurrency、Queue threshold、預熱時間與多目標成本。
- [ ] 把單機 RPS 拆成 concurrency 與服務時間分布，評估逐筆離散事件或 SimPy 實作。
- [ ] 將 Monte Carlo 上限由 500 擴充至離線 1,000+ runs，並加入平行運算與執行時間報告。
- [ ] 建立 pytest 自動化測試、固定 regression baselines 與 CI。
- [ ] 使用有效 API key 執行真正 OpenAI tool call，產生 12 筆案例的 Top-1、Top-3 與 no-match false-positive 指標。
- [ ] 使用真實壓測資料校準 worker、DB、gateway、warmup 與 retry 參數。
- [ ] 若取得去識別化逐筆事件時間，比較 Poisson、negative-binomial／Cox 與 Hawkes 的 out-of-sample fit。
- [ ] 實作持久化 idempotency key、委託狀態機與「已接受但未成交」語意；目前不接真實 DB。
- [ ] 串接公開新聞與市場行情，保留資料時間戳、來源與失敗降級機制。
- [ ] 加入認證授權、rate limiting、structured logging、metrics、trace、Docker 與部署設定。
- [ ] 與 API 呼叫端定稿 contract；目前舊 `後端.md` 的登入模型及範例格式已和下單版實作不一致。

## 參考資料

- [OpenAI Function Calling](https://developers.openai.com/api/docs/guides/function-calling)：Agent tool schema 與 tool-call 流程。
- [Kubernetes Horizontal Pod Autoscaling](https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/)：反應式擴容與控制迴路背景。
- [AWS Builders' Library: Timeouts, retries and backoff with jitter](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/)：逾時、重試放大、backoff 與 jitter。
- [AWS Builders' Library: Avoiding insurmountable queue backlogs](https://aws.amazon.com/builders-library/avoiding-insurmountable-queue-backlogs/)：Queue backlog 與系統恢復風險。

這些來源用來支持工程機制與風險設計，不是合成 RPS 倍率或券商真實容量的直接證據。
