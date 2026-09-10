# `orca serve` 的 Web artifact 發布邊界

在 `orca serve` ADE execution profile 中，agent 產生的 HTML 位於 remote runtime VM，guest path 無法直接被使用者 browser 開啟。決定由本 repo 提供 `ade publish-html` CLI，將單檔 HTML 或多檔案 bundle 發布到由 systemd 管理的固定 HTTP publisher，使用固定名稱覆蓋既有內容，並回傳可由使用者 browser 存取的 Artifact access URL。publisher 使用 HTTP port `80`，HTTPS 不納入本次範圍；拿到 URL 的人都能讀取內容，刪除由 CLI 手動執行。

## Consequences

- `shared-local` provider 維持 loopback 邊界；Web artifact publisher 是獨立的 runtime 服務，不放寬既有 Provider manifest 規則。
- publisher 必須限制在 artifact root、阻擋 path traversal、關閉 directory listing，並支援單檔 HTML 與含 `entrypoint` 的 bundle。
- 固定名稱採最後發布者覆蓋前一份內容。不同 workspace 共用名稱，可能互相覆蓋。
- HTTP 內容只適合公司內部網路。連線沒有 TLS 保護。
- `orca serve` 專用行為放在 Codex `config.toml` 的 `developer_instructions`；共用的 Workflow Pack prompt 不修改。publisher 無法使用時回報 `publish blocked`，不把 guest path 當成 browser URL。
