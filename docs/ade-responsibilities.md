# ADE 職責劃分

ADE 服務個人開發者，由 `agent-dev-environment` monorepo 組裝可安裝 distribution，共享 Core contract 並以 Host adapters 支援不同 hosts。Runtime 與 Workflow Pack 共用 repository，分別擁有自己的模組與使用入口；現行決策見 ADR-0008。KEN-9 的同步責任沿用這條邊界。

## KEN-9 Plane 到 Linear 同步

| 元件 | 擁有的責任 | 不擁有的責任 |
| --- | --- | --- |
| `ade/core.py` | 安裝、設定合併、grant、generation 與 rollback | Plane/Linear 業務規則 |
| `ade/cli.py`、`ade/scheduler.py` | 手動 sync 入口、三次每日排程、user-level process manager 檔案 | API payload mapping |
| `ade/sync.py` sync engine | assignment scope、field/status mapping、source identity、idempotency、逐筆錯誤隔離與 audit | Host-specific MCP 設定 |
| `PlaneApiClient` | Plane REST endpoint、認證 header、pagination、Plane payload normalization | Linear issue identity |
| `LinearApiClient` | Linear GraphQL request、issue create/update、workflow state 與 label lookup | 排程與 local mapping policy |
| `SyncStateStore` | ADE local state lock、Plane 到 Linear mapping、run record | credential、完整 API payload |
| Host adapter | Claude Code、Codex、OpenCode 的設定接入 | 同步排程與 provider state |

Plane 是來源，Linear 是個人追蹤目標。Source identity 使用 Plane workspace 與 immutable issue ID，不用 title 或 display identifier。解除指派時保留既有 Linear issue，避免 assigned-only response 造成破壞性誤判；仍在 assignment scope 的 terminal issue 會同步到 Linear terminal state。

## Ownership

| 元件 | 擁有的責任 | 對外邊界 |
| --- | --- | --- |
| Composition repository | 元件選型、lockfile、installer、設定合併、使用者授權、更新與 rollback、health checks | 組裝與驗證完整 ADE |
| `skills/`、`prompt/` Workflow Pack | Skills、個人 prompts、工作流程、review 範圍與結果處理政策 | 宣告需要的能力，呼叫 provider |
| Host adapter | 將 Core contract 對應到 host 設定、invocation 與連線方式 | 提供 host-specific 整合 |
| Capability provider | 專用能力、業務狀態與能力健康狀態 | 透過宣告的介面提供能力 |
| Alibaba OpenCodeReview | 目標設計中的預設 Review engine | 由整合層提供 review request/result contract |
| Headroom | 選用的 context/token 處理能力 | 預設關閉，依 host 或 profile 啟用 |
| MCP | Host/client 與 provider 之間的通訊協定 | 實際能力、權限與 process ownership 仍由對應元件負責 |

個人 prompts 的可散布內容由 Workflow Pack 維護；使用者的選擇與覆寫放在 user config。Workspace 設定承載專案需求。憑證不得放入 Workflow Pack 或版本化的共享設定。

## 設定與 lifecycle

一般設定優先順序為 `ADE defaults < user config < workspace config`，但權限與資料出口以使用者授權為上限。Provider manifest 必須符合 schema，宣告能力、permissions、health check 與 lifecycle。ADE 提供少量版本固定的 defaults，第三方擴充採明確納管。

| Lifecycle | 啟動與停止 owner | 多 host 行為 |
| --- | --- | --- |
| `host-spawned` | Host | 各自啟動所需 instance |
| `shared-local` | ADE runtime | 連接同一個受管理服務 |

Provider 的業務狀態與 adapter 的整合狀態應分開。Adapter 可保存連線或 session 對應資訊；provider 保有其原生 session 與結果。避免在兩處各自實作 finding counter 或其他業務判斷。

## 現況與遷移邊界

目前 ADR-0002 定義 `/implement` 使用 Standards/Spec review，OCR variant 保有獨立契約。ADR-0006 選定 ADE 的預設 engine，尚未取代這份現行 workflow 契約，也未證實既有 forked adapter 與選定上游相容。實作遷移前需核對介面與狀態 ownership，決定後才更新 skill 行為。

目前 `scripts/link-skills.sh` 仍是本機開發工具。`ade/` 已實作 lockfile、plan/apply、checksum 驗證、原子 generation 切換、rollback、設定合併、啟動授權與三種 host 的 workspace attach。原始 host 設定保留，不同內容的同名 entry 會停止接入。Bundled Workflow Pack 直接取自同 repo，內容 digest 納入 plan。

已實測在 ADE 專用目錄安裝 Alibaba OpenCodeReview `1.11.4`。Headroom 預設關閉，尚未安裝；LLM endpoint 與 credential 尚未設定，因此沒有執行模型 review。ADE 目前落實設定與啟動授權，外部 provider 仍須受信任；尚未提供 OS sandbox 或封包層資料出口限制。這些限制必須和 local-first 目標一起閱讀，不能將 permission manifest 視為執行期隔離保證。

## 決策依據

- [ADR-0008：monorepo 與模組 ownership](./adr/0008-ade-monorepo.md)
- [ADR-0005：provider 與資料邊界](./adr/0005-curated-providers-and-local-first-data.md)
- [ADR-0006：預設 Review engine](./adr/0006-alibaba-open-code-review-default.md)
- [ADR-0007：設定、更新與 lifecycle](./adr/0007-ade-configuration-and-runtime.md)
