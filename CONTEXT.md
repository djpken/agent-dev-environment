# Agent development environment context

這個 monorepo 收納 Agent Development Environment 的 runtime、skills 與 prompt，並定義環境、issue synchronization 與 review workflow 語言。

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

**Orca repository**：
提供 ADE 執行 host 與 remote runtime 的 `stablyai/orca` repository。ADE runtime、Workflow Pack 與 provider composition 仍由本 repo 擁有。
_Avoid_: ADE runtime repository、Workflow Pack repository

**ADE execution profile**：
決定 ADE 工作負載使用哪個 Orca execution host 的命名設定。目前支援 `orca serve` 與 `Local`：前者使用 headless Orca runtime，後者使用目前主機。
_Avoid_: provider lifecycle、provider profile

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

## Remote HTML publication

**Web artifact**：
由 agent 產生、供使用者在一般 browser 讀取的 HTML 產物，可以是單一 HTML 檔案，也可以是包含相依資源的 bundle。
_Avoid_: provider artifact、guest path

**Artifact publication**：
在 `orca serve` ADE execution profile 中，讓 Web artifact 可由使用者 browser 存取的交付行為。
_Avoid_: local file output、preview session

**Artifact access URL**：
使用者 browser 開啟 Web artifact 的 HTTP URL。取得 URL 的人都能讀取對應內容。
_Avoid_: guest path、provider URL

**Guest path**：
只在 remote runtime 所在 VM 有效的檔案路徑，不能當成使用者的 Artifact access URL。
_Avoid_: artifact access URL、browser URL

## ADE issue synchronization

**Plane issue**：公司 Plane 中的原始工作項目，也是同步資料的來源。
_Avoid_: ticket、mirror issue

**Linear issue**：個人 Linear workspace 中追蹤 Plane issue 的工作項目。
_Avoid_: copy、duplicate issue

**Source identity**：由來源 provider、workspace 與 Plane issue ID 組成，能在多次同步與本機 state 遺失後辨識同一個 Plane issue 的穩定身份。
_Avoid_: title matching、display identifier

**Assignment scope**：目前指派給指定使用者的 Plane issues。仍在此範圍內的 completed 或 canceled issue 也屬於同步來源；解除指派後不再產生新的 Linear 更新。
_Avoid_: all issues、active-only issues

**Sync run**：一次手動或排程觸發的 Plane issue 讀取與 Linear reconciliation。
_Avoid_: batch、job

**Source trace**：Linear issue 中可回到 Plane 原始 issue 的 URL、identifier 與 project 資訊。
_Avoid_: backlink、provenance blob

## Prompts

**本地改編 prompt**：Workflow Pack 中參考外部來源、翻譯並依本地偏好調整的 prompt；其內容由本地維護者決定。
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
