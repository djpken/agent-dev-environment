# ADR-0006：以 Alibaba OpenCodeReview 作為預設 Review engine

- 狀態：接受
- 日期：2026-09-04

ADE 選用 `alibaba/open-code-review` 作為預設 Review engine，負責 Git diff 或完整檔案分析、coverage 與 structured findings。Workflow Pack 只描述何時 review、範圍與結果處理政策；Composition repository 的 adapter 負責 process lifecycle、同步等待、session persistence 與 host-specific tool contract。OpenCodeReview 連接外部 MCP resources 的 client 能力不取代 ADE 的 provider 管理或 Core contract。
