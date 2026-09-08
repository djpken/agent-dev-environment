# ADR-0004：以單一可安裝 distribution 交付 ADE

- 狀態：repository 分離決策已由 ADR-0008 取代；個人開發者優先與跨 host 方向保留
- 日期：2026-09-04

以下保留當時的分 repo 決策。現行 repository ownership 見 [ADR-0008](./0008-ade-monorepo.md)。

Agent Development Environment 以個人開發者為第一優先，交付一套可直接安裝、版本固定且能重現的 distribution。新的 Composition repository 擁有 installer、版本鎖定、Host adapters、Provider manifests 與 health checks；`plugins-zh-tw` 只作為 Workflow Pack 提供 skills、prompts 與 workflow policies。跨 host 行為由 Core contract 定義，Claude Code、Codex、OpenCode 等 agent host 透過各自的 Host adapter 接入。這個選擇接受較明確的元件選型與升級責任，換取安裝後即可使用、跨 host 行為一致，以及內容與 runtime 各自獨立的發布週期。
