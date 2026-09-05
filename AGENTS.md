## 專案定位

**OpeningGuard AI** — 黑客松期間專案，目標是快速 demo，不是正式生產系統。

**要解決的問題**：基於前一天的交易資料、新聞等營運訊號，用 LLM 分析並預測隔日開盤流量，讓工程團隊能提前預熱容量，避免開盤尖峰把系統沖垮。

**架構**：前後端分離。

- **backend/**：已可執行的 FastAPI 服務。合成流量 Monte Carlo 模擬 + 三個 OpenAI judge 從固定風險目錄選 `risk_id`/`severity` + 三種容量策略（固定／反應式／預測式預熱）比較。完整技術設計、假設與限制見 [`backend/README.md`](backend/README.md) 與 [`backend/docs/後端.md`](backend/docs/後端.md)，接手前必讀，此處不重複。後端不需要進行測試。
- **frontend/**：目前是空目錄，尚未開工。規劃技術棧 React + shadcn + Recharts + ReactFlow，分工如下：
  - **ReactFlow**：worker／queue／Database／gateway 節點的即時模擬動畫，把處理延遲與系統壓力隨模擬時間的變化畫出來（對應 `backend/README.md` 的系統流程圖，做成互動版本，而非靜態 mermaid）。
  - **Recharts**：事後統計圖表——P95 延遲、壅塞機率（含 Wilson 95% CI）、worker-minutes 成本曲線等，對應後端 `/api/simulate` 回傳的 `scenarios` 與 Notebook 產出的指標。

黑客松限制：demo 優先，`backend/README.md` 中列出的「未完成與已知限制」（正式 evidence 校準、認證授權、CI 等）在 demo 期間刻意不處理。
