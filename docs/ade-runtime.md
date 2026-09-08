# Agent Development Environment

個人開發者的 local-first ADE monorepo。Workflow Pack 位於同 repo 的 `skills/` 與 `prompt/`；`ade/` 管理版本、安裝、設定、provider grants、host adapter 輸出、issue sync 與 user-level schedule。

目前為 `0.1.0`，支援 Linux/macOS、Python 3.11 以上與 Git。使用 `uv.lock` 固定 ADE 的 Python 相依。Windows 尚未實作原子 symlink 切換與 process lifecycle。

## 使用

在本 repo 執行：

```bash
uv sync --frozen
uv run --frozen ade plan --user user.example.json
uv run --frozen ade apply --user user.example.json --plan-id <plan_id>
uv run --frozen ade doctor
uv run --frozen ade status
uv run --frozen ade rollback
```

`apply` 必須提供本次 `plan` 的 ID。設定、lockfile 或 current generation 改變後，舊計畫會被拒絕。下載先檢查 SHA-256，再檢查 OCR 版本；完整 stage 成功後以單次 symlink replacement 啟用。舊 generation 保留供 rollback，不清除 provider 業務資料。

預設資料目錄為 `~/.local/share/ade`。所有指令都可在子指令前加 `--root /absolute/path`，選擇另一個隔離 installation。預設從 lockfile 所在的 monorepo 讀取 Workflow Pack，也可用 `--workflow-repo` 指定位置。Bundled 模式包含目前檔案內容，plan ID 納入內容與 executable bits 的 digest；修改後須重新 plan。Generation 保存實際內容與 digest，更新後不依賴原始 checkout。舊的固定 Git revision 模式仍可用於既有 lockfile。

`user.example.json` 是設定範本。實際設定請使用被 Git 忽略的 `user.json`；只有 `providers` 與 `prompts` 可放進 `--workspace-config`。Workspace 無法改寫 grants、endpoint 或註冊 extension command。

## Plane 到 Linear

啟用 `plane-linear-sync` 後，用 repo 外的 owner-only env file 手動執行：

```bash
uv run --frozen ade sync --provider plane-linear-sync --env-file ~/.config/ade/plane-linear-sync.env
uv run --frozen ade schedule install --provider plane-linear-sync --env-file ~/.config/ade/plane-linear-sync.env --enable
```

排程由 `ade/scheduler.py` 渲染 systemd user timer 或 macOS launchd，時間固定為 `08:00`、`12:00`、`17:00` 的 local timezone。排程 command 只包含 env file path，不含 token。同步的 mapping 與 JSONL run record 位於 `~/.local/share/ade/state/plane-linear-sync/`，檔案權限由 runtime 設為 owner-only。

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
uv run --frozen ade attach --host codex --workspace /path/to/project
uv run --frozen ade attach --host codex --workspace /path/to/project --apply
```

`--host` 可選 `claude`、`codex`、`opencode`。Attach 合併 ADE MCP entries、保留既有無關設定，並建立指向 `current` 的 skill symlinks。Claude Code 使用 `.claude/skills`，另外兩個 host 使用 `.agents/skills`。同名 skills 或不同內容的 MCP entries 會中止整次 attach。原始 config 備份存放在 ADE 的 `attachments/`，不改寫全域 host 設定。切換 generation 後 skills 自動跟隨；provider 設定有變更時需重新 attach，衝突會明確回報。停用 provider 後，舊 stdio entry 經 wrapper 呼叫仍會拒絕執行。

預設接入 promoted skills 與 `base`、`implement`、`code-review`、`wait-what`；尚未遷移的 `code-review-ocr` 不會接入。測試覆蓋三種設定格式及接入，未向三種 host 發送模型請求。個人 prompt 產物保留為明確選用檔案，不自動寫入專案 `AGENTS.md`。

## Review

先在 user config 設定明確 `llm_endpoint` 與 OCR grants，再重新 plan/apply。只有執行 review 時才從指定環境變數讀取 credential；credential 不寫入 lockfile、generation 或 CLI arguments。

```bash
uv run --frozen ade review --workspace /path/to/repo --base <commit> --head HEAD --model <model> --key-env OCR_LLM_TOKEN
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
uv run --frozen python -m unittest discover -s tests -v
```

測試包含設定合併、提權阻擋、manifest 驗證、下載損壞、version mismatch、失敗保留 current、過期 plan、rollback、三種 host 設定格式、環境變數過濾、review range/endpoint mapping、Plane/Linear mapping、排程與同步錯誤隔離。

官方介面依據：

- [Alibaba OpenCodeReview v1.11.4](https://github.com/alibaba/open-code-review/tree/v1.11.4)
- [OCR endpoint resolver](https://github.com/alibaba/open-code-review/blob/v1.11.4/internal/llm/resolver.go)
- [Headroom MCP manifest](https://github.com/headroomlabs-ai/headroom/blob/main/server.json)
- [Codex MCP](https://developers.openai.com/codex/mcp)
- [Claude Code MCP](https://code.claude.com/docs/en/mcp)
- [OpenCode MCP](https://opencode.ai/docs/mcp-servers/)
