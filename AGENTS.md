## 專案定位

**OpeningGuard AI** — 黑客松期間專案，目標是快速 demo，不是正式生產系統。

**要解決的問題**：基於前一天的交易資料、新聞等營運訊號，用 LLM 分析並預測隔日開盤流量，讓工程團隊能提前預熱容量，避免開盤尖峰把系統沖垮。

**架構**：前後端分離。

- **backend/**：已可執行的 FastAPI 服務。合成流量 Monte Carlo 模擬 + 三個 OpenAI judge 從固定風險目錄選 `risk_id`/`severity` + 三種容量策略（固定／反應式／預測式預熱）比較。完整技術設計、假設與限制見 [`backend/README.md`](backend/README.md) 與 [`backend/docs/後端.md`](backend/docs/後端.md)，接手前必讀，此處不重複。後端不需要進行測試。
- **frontend/**：目前是空目錄，尚未開工。規劃技術棧 React + shadcn + Recharts + ReactFlow，分工如下：
  - **ReactFlow**：worker／queue／Database／gateway 節點的即時模擬動畫，把處理延遲與系統壓力隨模擬時間的變化畫出來（對應 `backend/README.md` 的系統流程圖，做成互動版本，而非靜態 mermaid）。
  - **Recharts**：事後統計圖表——P95 延遲、壅塞機率（含 Wilson 95% CI）、worker-minutes 成本曲線等，對應後端 `/api/assessments` 回傳的 `scenarios` 與 Notebook 產出的指標。

黑客松限制：demo 優先，`backend/README.md` 中列出的「未完成與已知限制」（正式 evidence 校準、認證授權、CI 等）在 demo 期間刻意不處理。

## Hackathon Scope & Stability

本專案是為黑客松 demo 準備的 prototype，不是 production system。所有實作與修改都應以「快速完成可展示的 end-to-end demo」為最高優先。

除非是讓 demo 無法正常執行的必要處理，**不要為了 production-grade 穩定性而額外加入複雜的防禦性或可靠性程式碼**。例如：

* 不需要為 API 加入複雜的 retry、exponential backoff、circuit breaker 等機制。
* 不需要設計 graceful degradation、fallback chain 或多層級故障轉移。
* 不需要為暫時性的服務錯誤建立完整的 recovery / self-healing 機制。
* 不需要加入 production-grade timeout、rate limiting、connection pooling 等基礎設施，除非 demo 本身確實需要。
* 不需要為所有邊界情況建立完整的 error handling framework。
* 不需要為尚未發生的高併發、分散式故障或 partial failure 設計複雜架構。
* 不需要為了「未來可能需要」而抽象出過度通用的 interface、adapter 或 configuration layer。

在遇到錯誤時，優先採用簡單、直接、容易理解的處理方式。若某個問題不影響 hackathon demo 的主要流程，可以明確記錄為 limitation，而不是為了解決它大幅增加程式碼與架構複雜度。

判斷是否要加入一項工程機制時，優先問：

1. 這是否是 demo 的核心功能？
2. 沒有它，demo 是否會無法執行或無法展示？
3. 它是否能在合理時間內完成？

如果答案不是明確的「是」，通常不要加入。

**不要 over-engineer。** 本專案刻意接受 prototype 等級的可靠性、錯誤處理與架構簡化。工程時間應優先投入在 OpeningGuard AI 的核心展示價值：LLM risk assessment、隔日開盤流量預測、容量策略比較，以及前端的即時系統模擬與事後分析視覺化。
