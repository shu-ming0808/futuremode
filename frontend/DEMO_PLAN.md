# OpeningGuard AI — 前端 Demo 規劃

黑客松 demo 用,目標是講清楚兩件事:predictive prewarm 比 fixed/reactive 更早控制壅塞、DB／gateway 硬上限時加 worker 沒用。前端不重算 Monte Carlo 統計,只做視覺化與單次演示動畫。

**Monte Carlo 統計比較(`scenarios[]` 五圖)不是前端展示重點,已從頁面移除。** 前端集中呈現單次模擬的效果(第 4、5 步),`recommended` 卡片本身的數字（來自後端 Monte Carlo）已足夠佐證建議方案,不需要額外的統計圖表區塊。

## 頁面排列順序

1. **情境選擇**:三顆固定按鈕 `normal` / `high_pressure` / `downstream_bottleneck`,各帶情境預設 `operation_note`,不留自由輸入框(降低現場出錯風險)。固定 `use_agent=true`(risk judge 是故事核心,不給關掉)。`profile` 固定 `demo`,不對外開放。

2. **風險卡片區**:按鈕觸發 `POST /api/assessments` 後顯示 `risks[]`。每張卡預設只顯示 `title` + `severity` 顏色標籤(綠/黃/紅/灰=uncertain),點擊展開 `matched_input_text`、`source_url`、`effects`(套用到模擬的實際倍率)。`requires_human_review=true` 時頁面頂部加警告列。

3. **建議方案卡**(頁面最醒目位置):顯示 `recommended.workers` + 預期 `p95_latency_ms`/`congestion_probability`/`cost`。若 `recommended=null`(`warning_code=no_feasible_plan`),改為紅色「無安全方案,需 DB/gateway 擴容或限流」警告,隱藏下一步的執行按鈕。

4. **觸發單次模擬**(點擊建議卡):呼叫 `POST /api/single-run-trace`。後端只回傳到達曲線(`arrivals[]`,三策略共用同一份)＋三策略固定參數(`current_workers`/`target_workers`/`queue_threshold`/`warmup_seconds`)＋共用 `node_params`(worker concurrency/efficiency/mean_service_time、db/gateway capacity_per_tick),不回傳任何逐 tick 結果:
   - `fixed`、`reactive` 兩欄用情境固定的 `current_workers`/`target_workers`。
   - `predictive` 欄位用 `recommended.workers`(後端已篩選出的最低成本安全值)當 `target_workers` 覆寫,不是情境檔裡的固定值。
   - 前端 `src/lib/simulation/engine.ts`(`SingleRunEngine`)對每個策略吃這份 arrivals + 參數,自己跑 queue/pod 狀態機與三種策略的 worker 擴縮規則,逐 tick 算出 queue 長度、worker 數、DB/gateway utilization、P95/timeout 供動畫與統計卡片使用。

5. **ReactFlow 管線動畫**(Tabs 切換三策略):每個策略一套放大版管線 `Queue → Worker → DB → Gateway`,末端分岔 `accepted` / `timeout` 兩條出路,節點顏色反映該 tick 的壓力(queue 長度、utilization)。預設顯示 `predictive_prewarm`,可切到 `fixed_capacity`/`reactive_autoscaling` 對照。

6. **時序線圖**(三策略疊圖,隨播放進度逐格畫出):到達量(三策略共用同一份流量)、queue 長度、rolling P95 延遲,三張小圖橫排。曲線只畫到目前播放的 tick,不是一次全部畫完。

## 後端現況(已實作,見 [`backend/docs/前端需求-單次逐tick模擬.md`](../backend/docs/前端需求-單次逐tick模擬.md))

- `/api/single-run-trace`:輸入 `scenario` + 三策略各自的 worker 數覆寫值(predictive 用 `recommended.workers`),輸出到達曲線 + 固定策略/節點參數,不含任何逐 tick 結果——queue/pod 狀態機與統計改由前端 engine 自己算。
- 不改動現有 `/api/assessments` 的 Monte Carlo 統計聚合邏輯與 response schema。

## 已排除的設計

- 前端自行重寫合成到達率公式(`generate_arrival_trace` 的 lognormal/AR(1) 群聚擾動仍由後端算,前端只吃現成 `arrivals[]`);queue/pod 狀態機與三策略擴縮規則則已改為前端 engine 自己跑(見上方第 4 步),不 replay 後端結果。
- 「先跑一次 baseline、使用者確認後再重打一次 API」的兩階段真實重打流程(改用單次 API 回傳的資料驅動三段式動畫)。
- `use_agent` 開關、`operation_note` 自由輸入框、`profile` 選擇(全部固定,減少 demo 現場變數)。
