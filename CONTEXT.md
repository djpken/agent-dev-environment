# Agent development environment context

這個 monorepo 收納 Agent Development Environment 的 runtime、skills 與 prompt，並定義環境與 review workflow 語言。

## Environment

**Agent Development Environment (ADE)**：
個人開發者可直接安裝的完整 agent 工作環境，讓支援的 agent hosts 使用一致的 workflows 與 capabilities。
_Avoid_: plugin collection、skills repo

**Core contract**：
所有支援的 agent hosts 共同遵守、且不綁定單一 host 的行為契約。
_Avoid_: common config、shared prompt

**Host adapter**：
將特定 agent host 的 invocation、設定與生命週期對應到 Core contract 的邊界元件。
_Avoid_: plugin、integration script

**Composition repository**：
同時收納 ADE runtime 與 Workflow Pack，定義完整 distribution、元件版本與整合關係的 canonical source。
_Avoid_: plugin repo、dotfiles repo

**Workflow Pack**：
由 ADE 安裝的 skills、prompts 與 workflow policies 集合；可獨立安裝，與 runtime 共存於同一個 monorepo。
_Avoid_: plugin、ADE distribution

**Capability provider**：
獨立擁有一項專門能力及其執行狀態，並透過受控介面供 ADE 使用的元件。
_Avoid_: tool、plugin、MCP server

**Provider manifest**：
ADE 用來判斷 Capability provider 身分、能力、權限需求與健康狀態的宣告。
_Avoid_: MCP config、plugin metadata

**Review engine**：
負責程式碼分析、coverage 與 structured findings 的 Capability provider。
_Avoid_: code-review skill、review workflow

## Prompts

**本地改編 prompt**：
Workflow Pack 中參考外部來源、翻譯並依本地偏好調整的 prompt；其內容由本地維護者決定。
_Avoid_: 上游同步副本、逐字翻譯版

## Baseline

**Base manifest**：由 `/base` 建立的固定 review 起點與 task source。
_Avoid_: moving ref、臨時 baseline

**Review range**：從固定 review 起點到目前 revision 的變更範圍，並排除 review 自己產生的狀態。
_Avoid_: workspace review、single-commit review

## Review variants

**Standards/Spec review**：同時檢查程式碼是否符合 repo standards，以及是否符合 Base manifest 提供的 task source。
_Avoid_: 單一總分 review

**OCR review**：依賴 OCR MCP 的 review variant，保留自己的 findings 與 counter 狀態。
_Avoid_: 單軸 review、Standards/Spec review

**Review variant**：共享 Base manifest、review range 與 task source contract 的獨立 review 入口。
_Avoid_: review mode、alias
