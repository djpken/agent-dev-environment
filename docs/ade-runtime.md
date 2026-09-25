# Agent Development Environment

個人開發者的 local-first ADE monorepo。Workflow Pack 位於同 repo 的 `skills/` 與 `prompt/`；Go CLI/runtime 管理版本、安裝、設定、provider grants、host adapter 輸出、issue sync 與 user-level schedule。

目前為 `0.1.0`，Go runtime 支援 Linux/macOS 與 Git，使用 `go.mod` 固定 ADE 相依。Windows 尚未實作原子 symlink 切換與 process lifecycle。

## Orca 整合與 execution profiles

ADE 執行環境使用 [`stablyai/orca`](https://github.com/stablyai/orca) repository 提供的 Orca execution host。本 repo 的 `cmd/ade/` 與 `internal/ade/` 擁有 Go runtime，`skills/` 與 `prompt/` 擁有 Workflow Pack；Orca 負責承載與連接工作負載。

目前有兩個 ADE execution profile：

| Profile | 執行位置 | 使用方式 |
| --- | --- | --- |
| `orca serve` | headless Orca runtime | 由 `orca serve` 啟動不開桌面視窗的 Orca server，透過 pairing 讓 ADE client 連線。適合 VM、遠端 Linux 或長時間執行的環境。 |
| `Local` | 目前執行 Orca 的主機 | 直接在本機執行工作負載，不需要另外連接 `orca serve`。適合本機開發與測試。 |

Profile 只選擇工作負載的 execution host；兩個 profile 共用同一套 ADE Workflow Pack、provider manifest 與授權規則。`orca serve`／`Local` 也不要和 provider manifest 的 `host-spawned`／`shared-local` 混用，前者描述執行位置，後者描述 provider process 的生命週期 owner。

## 使用

在本 repo 執行：

```bash
mkdir -p .ade/bin
go build -o .ade/bin/ade ./cmd/ade
.ade/bin/ade plan --user user.example.json
.ade/bin/ade apply --user user.example.json --plan-id <plan_id>
.ade/bin/ade doctor
.ade/bin/ade status
.ade/bin/ade rollback
```

`apply` 必須提供本次 `plan` 的 ID。設定、lockfile 或 current generation 改變後，舊計畫會被拒絕。下載先檢查 SHA-256，再檢查 OCR 版本；完整 stage 成功後以單次 symlink replacement 啟用。舊 generation 保留供 rollback，不清除 provider 業務資料。

預設資料目錄為 `~/.local/share/ade`。所有指令都可在子指令前加 `--root /absolute/path`，選擇另一個隔離 installation。預設從 lockfile 所在的 monorepo 讀取 Workflow Pack，也可用 `--workflow-repo` 指定位置。Bundled 模式包含目前檔案內容，plan ID 納入內容與 executable bits 的 digest；修改後須重新 plan。Generation 保存實際內容與 digest，更新後不依賴原始 checkout。舊的固定 Git revision 模式仍可用於既有 lockfile。

`user.example.json` 是設定範本。實際設定請使用被 Git 忽略的 `user.json`；只有 `providers` 與 `prompts` 可放進 `--workspace-config`。Workspace 無法改寫 grants、endpoint 或註冊 extension command。

## Plane 到 Linear

啟用 `plane-linear-sync` 後，用 repo 外的 owner-only env file 手動執行：

```bash
.ade/bin/ade sync --provider plane-linear-sync --env-file ~/.config/ade/plane-linear-sync.env
.ade/bin/ade schedule install --provider plane-linear-sync --env-file ~/.config/ade/plane-linear-sync.env --enable
```

排程由 `internal/ade/scheduler.go` 渲染 systemd user timer 或 macOS launchd，時間固定為 `08:00`、`12:00`、`17:00` 的 local timezone。排程 command 只包含 env file path，不含 token。同步的 mapping 與 JSONL run record 位於 `~/.local/share/ade/state/plane-linear-sync/`，檔案權限由 runtime 設為 owner-only。

完整欄位、狀態、credential 與錯誤規則見 [ADE Plane 到 Linear 同步](./ade-plane-linear-sync.md)。

## 元件

| 元件 | 本版行為 |
| --- | --- |
| Workflow Pack | 從 monorepo 打包，以內容 digest 驗證 plan，保留原本 skill 內容 |
| OCR | 安裝官方 `1.11.4` binary，支援四種 Linux/macOS 架構 |
| Headroom | `0.36.5` manifest，預設關閉；啟用前需有相同版本的 CLI |
| Plane 到 Linear | `plane-linear-sync` provider，預設關閉；依指派範圍同步到個人 Linear workspace |
| Claude Code | `current/exports/claude.mcp.json` |
| Codex | `current/exports/codex.toml` |
| OpenCode | `current/exports/opencode.json` |
| 個人 prompt | 依設定選取 pack 的 `prompt/` 檔案，合併到 `current/personal-prompt.md` |

Host exports 為原生設定格式，stdio entries 經 ADE wrapper 驗證 grants 與版本，再啟動工具。CLI-only OCR 透過 `ade review` 使用，不偽裝成上游沒有提供的 MCP server。既有 `/code-review-ocr` 的 forked MCP contract 尚未遷移。

使用 `attach` 預覽並接入指定 workspace：

```bash
.ade/bin/ade attach --host codex --workspace /path/to/project
.ade/bin/ade attach --host codex --workspace /path/to/project --apply
```

`--host` 可選 `claude`、`codex`、`opencode`。Attach 合併 ADE MCP entries、保留既有無關設定，並建立指向 `current` 的 skill symlinks。Claude Code 使用 `.claude/skills`，另外兩個 host 使用 `.agents/skills`。同名 skills 或不同內容的 MCP entries 會中止整次 attach。原始 config 備份存放在 ADE 的 `attachments/`，不改寫全域 host 設定。切換 generation 後 skills 自動跟隨；provider 設定有變更時需重新 attach，衝突會明確回報。停用 provider 後，舊 stdio entry 經 wrapper 呼叫仍會拒絕執行。

預設接入 promoted skills 與 `base`、`implement`、`code-review`、`wait-what`；尚未遷移的 `code-review-ocr` 不會接入。測試覆蓋三種設定格式及接入，未向三種 host 發送模型請求。個人 prompt 產物保留為明確選用檔案，不自動寫入專案 `AGENTS.md`。

## Web artifact 發布

`orca serve` profile 中，agent 產生的 HTML 可用 `ade publish-html` 發布給使用者的 browser。Publisher 由 systemd 管理的 nginx 提供 HTTP port `80`，固定使用 `/artifacts/<name>/` path routing。拿到 URL 的人都能讀取內容；固定名稱由最後一次發布覆蓋，可透過 CLI 或登入管理頁面後刪除。

Publisher root 與 browser origin 可用環境變數設定：

```bash
export ADE_WEB_ARTIFACT_ROOT=/var/lib/ade/web-artifacts
export ADE_WEB_ARTIFACT_BASE_URL=http://172.16.240.41:80
```

發布單檔 HTML 或 bundle：

```bash
.ade/bin/ade publish-html publish report.html --name architecture
.ade/bin/ade publish-html publish report-bundle/ --name architecture --entrypoint index.html
.ade/bin/ade publish-html delete architecture
```

單檔 HTML 會以 `index.html` 作為 entrypoint；bundle 可包含 HTML、PNG、CSS 與 JS。CLI 會先檢查 `http://127.0.0.1:80/healthz`，服務不可用時回報 `publish blocked`，不把 guest path 當成 browser URL。nginx 設定樣板位於 [`deploy/ade-web-artifacts/`](../deploy/ade-web-artifacts/)。

管理頁面也可共用這套發布功能。在 ADES 的 manifest 設定
`artifacts.enabled: true`、`artifact_root` 與 `base_url` 後，登入即可列出及開啟既有作品；
operator／admin 可上傳 HTML 或完整資料夾、選擇首頁、確認覆蓋與刪除。
上傳限制為 200 個檔案、合計 10 MiB。CLI 與管理頁面使用同一個作品目錄。
作品維持 HTTP port `80`，管理頁面使用不同 origin；發布內容不會在管理頁面內執行。
既有 manifest 預設不啟用此功能，設定與 API 詳見
[管理服務的網頁發布說明](../deploy/agent-environment/README.md#web-publishing-from-the-dashboard)。

## ADES · Agent Development Environment Service

ADES 的 Skills 與 MCP 手動更新檢查方式見[監控文件](./agent-assets-monitoring.md)。

ADES 是每台 VM 獨立的管理面，與 Web artifact publisher 分開。Dashboard 使用 React、TypeScript、Vite；ADES API、ADE CLI、provider lifecycle、host adapter、sync、scheduler、密碼授權與更新 runtime 都由 Go binary 提供。Go net/http server 維持同 origin JSON API，並直接提供 dashboard 靜態檔案。systemd、更新器與 root-owned helpers 都呼叫同一個 /usr/local/bin/ade，不再啟動 Python 或 Node server。Node.js 與 npm 僅在安裝時建置 React dashboard。ADES 以 allowlist 登錄 Orca、Codex、Capability provider 與 VM services，提供登入後可讀取的 health/dashboard、JSON API、session 授權的手動操作、持續生效的 Update policy 與 update history。元件可宣告 target_version_arg，讓核准的目標版本以 argv 傳給固定更新器；內建 Orca updater 會驗證 release manifest 的版本、大小與 SHA-512。

Linux + systemd VM 可用 [`deploy/agent-environment/`](../deploy/agent-environment/) 安裝。安裝需要 Go `1.27.1`、Node.js `22.12+` 與 npm。安裝器依 `go.mod` 編譯 Go binary，依 `web/package-lock.json` 建置 React dashboard，systemd 直接啟動 `/usr/local/bin/ade environment serve`。Go binary 同時提供 ADE CLI、管理 API 與更新 worker。預設管理面使用 HTTPS port `6790`，每日 `04:00 UTC+8` 執行更新，也就是 `20:00 UTC`；`Persistent=true` 會在 VM 錯過時間後補跑。更新由 root-owned helper 執行，Orca 與 Codex 會依 manifest 的固定 command 更新，MCP 與其他 service 只有登錄後才會被管理。

管理頁面也可由 Nginx 提供標準 `80／443` 入口：HTTP 首頁導向 HTTPS 管理頁面，作品維持 HTTP `80` 的獨立 origin。設定方式見 [Nginx 整合說明](../deploy/agent-environment/README.md#standard-http-and-https-entrypoints)。

管理面可設定 allow_http: true、tls: {} 與 HTTP backend，再由 Nginx 提供 HTTPS origin。密碼登入只接受直接 TLS，或由 loopback reverse proxy 傳入並標記 X-Forwarded-Proto: https 的請求。遠端 HTTP 即使位於可信 VPN 也不能登入。登入表單使用標準 username 與 current-password autocomplete，支援 1Password。

未登入時可查看公開的唯讀 GitHub Trending 與 Trendshift 即時排行；GET /api/v1/trending 只接受固定來源與每日／每週／每月期間，每個來源與期間在 VM 記憶體快取 45 秒，不保存候選或排行資料。帳號空白時，dashboard 提供一次性 admin 註冊或 WebDAV 備份匯入；註冊或匯入完成後不再開放公開註冊。管理員也能把環境設定加密後備份到 WebDAV。備份包含 ADES manifest、帳號雜湊與有效更新政策，使用 scrypt 與 AES-256-GCM；匯入僅接受相同 VM ID、source_root 與 state_root，並在服務設定變更時排程重啟。備份不包含 session、執行歷史、WebDAV 憑證或 TLS 私鑰。root-only `ade environment account set` 仍可建立或更新帳號供恢復使用。密碼以 salted scrypt hash 存在 root-owned accounts.json，bearer token 只以雜湊保存。登入 session 有效 12 小時，可在同分頁重新整理後驗證恢復；帳號更新會撤銷該帳號的 session 與待執行核准。Viewer 可讀取；operator 與 admin 可手動更新或重啟元件；只有 admin 可修改每日更新政策。更新器執行前仍會檢查 root-owned manifest、元件依賴與一次性核准。

Admin 可啟用、修改或停用每日更新。新排程沒有到期日，登出、session 過期或原設定者的身份異動都不會取消排程；執行時仍受 VM 與元件 allowlist 限制。設定保存在 root-owned `/etc/ade/agent-environment.auth/policy.json`，既有共用 state 中的簽署 policy 僅作為尚未替換時的相容來源。升級前須安裝 `agent-environment-authorize` helper 與 sudoers entry，再重啟管理服務，詳見 [部署文件](../deploy/agent-environment/README.md)與 [ADR-0014](adr/0014-password-authenticated-environment-session.md)。

`Environment status snapshot` 預設不發布，VM opt-in 後才用 `ade publish-html` 發布到獨立的 artifact publisher；snapshot 只含 sanitized health、版本與時間資訊。

## Review

先在 user config 設定明確 `llm_endpoint` 與 OCR grants，再重新 plan/apply。只有執行 review 時才從指定環境變數讀取 credential；credential 不寫入 lockfile、generation 或 CLI arguments。

```bash
.ade/bin/ade review --workspace /path/to/repo --base <commit> --head HEAD --model <model> --key-env OCR_LLM_TOKEN
```

Adapter 將 refs 解析成完整 commit SHA，要求 base 為 head 的 ancestor，再呼叫 OCR range review。上游管理自己的 review sessions 與 findings。實際模型呼叫需要使用者的 endpoint、model 與 credential；安裝及測試不會發送程式碼。

## 擴充與 lifecycle

User config 的 `extensions` 接受符合 `ade/provider.schema.json` 的 manifests。ID 必須唯一；command/health 使用 argv array，不經 shell。可用 permissions 為 `workspace-read`、`workspace-write`、`local-state`、`llm-network`、`external-network`、`listen-loopback`。

- `host-spawned`：`ade provider <id>` 驗證 grants 與版本後交接 process，host 擁有其生命周期。
- `shared-local`：`ade provider <id> --shared` 前景監督一個 loopback HTTP process，檢查 readiness、轉送停止訊號並回收 child；host 只連接 manifest URL。同一 endpoint 已使用時拒絕啟動。

Shared provider 的 HTTP endpoint 必須使用內部 loopback；不會對外公開 VM 服務。Shared services 需由操作者啟動，exports 不會偷偷啟動 daemon。停用或 rollback 不會遷移 provider 資料；仍在運行的 process 需要重新啟動以採用新設定。

## 安全邊界與限制

目前強制的是 **ADE 設定與啟動授權邊界**。Workspace 不能提高 grants、改變 endpoint 或注入 command；啟動前需驗證使用者授權。外部 provider 仍是受信任的本機程式，權限宣告不構成 OS sandbox 或封包層 egress enforcement。需要執行期硬隔離時，必須加入獨立 sandbox backend；目前不適合執行不可信 providers。

OCR endpoint 由 adapter 明確傳入；第三方 provider 必須自行遵守資料出口契約。通用 extensions 沒有自動翻譯各家的 LLM 設定。Headroom 尚未安裝，不會啟用 transparent proxy 或改寫 host 的 LLM 流量。Package manager 的 transitive dependencies 也不在 Headroom manifest 中鎖定。

## 驗證

```bash
go build ./...
uv run --frozen python -m unittest discover -s tests -v
```

現有 Python 行為測試涵蓋設定合併、提權阻擋、manifest 驗證、下載損壞、version mismatch、失敗保留 current、過期 plan、rollback、三種 host 設定格式、環境變數過濾、review range/endpoint mapping、Plane/Linear mapping、排程與同步錯誤隔離。Go runtime build 另由第一個命令驗證。

官方介面依據：

- [Alibaba OpenCodeReview v1.11.4](https://github.com/alibaba/open-code-review/tree/v1.11.4)
- [OCR endpoint resolver](https://github.com/alibaba/open-code-review/blob/v1.11.4/internal/llm/resolver.go)
- [Headroom MCP manifest](https://github.com/headroomlabs-ai/headroom/blob/main/server.json)
- [Codex MCP](https://developers.openai.com/codex/mcp)
- [Claude Code MCP](https://code.claude.com/docs/en/mcp)
- [OpenCode MCP](https://opencode.ai/docs/mcp-servers/)
