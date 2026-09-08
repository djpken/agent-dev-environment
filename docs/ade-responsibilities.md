# ADE 職責劃分

ADE 由同一個 monorepo 組裝 Workflow Pack、runtime、provider manifest 與 host adapter。KEN-9 的同步責任沿用這條邊界：

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
