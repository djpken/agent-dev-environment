# Agent instructions

本 repo 的 Agent skills 設定如下。`CLAUDE.md` 只引用本檔，請以本檔為準。

## Agent skills

### Issue tracker

Issues 與 specs 使用 GitHub Issues，操作使用 `gh` CLI。詳見 `docs/agents/issue-tracker.md`。

### Triage labels

使用預設 labels：`needs-triage`、`needs-info`、`ready-for-agent`、`ready-for-human`、`wontfix`。詳見 `docs/agents/triage-labels.md`。

### Domain docs

採用 single-context，使用根目錄 `CONTEXT.md` 與 `docs/adr/`。詳見 `docs/agents/domain.md`。

## Migrated skills collection

本 repo 已合併 `/Users/kunkun/Projects/skills` 的 skills collection。以下規則保留來源 repo 的維護約定，並補充目前 repo 原有的 root-level skills。

### Repo 性質

這是 `agent-dev-environment` monorepo。`skills/` 與 `prompt/` 擁有 Workflow Pack；`ade/` 擁有安裝、設定、provider 與 host 整合。`npm` 只用於 changesets；Python runtime 使用 `uv.lock`。

修改 runtime、安裝流程或 host adapter 時，執行 `uv run --frozen python -m unittest discover -s tests -v`。職責調整先讀 `docs/ade-responsibilities.md`；CLI 使用與限制見 `docs/ade-runtime.md`。

Workflow Pack 可以描述 MCP 使用政策與工具介面；版本、credentials、process lifecycle 與重連由 runtime 或 adapter 負責。Provider 擁有業務狀態，adapter 僅保存整合所需的狀態。

### 常用指令

- `npx changeset`：記錄準備發布的變更。
- `npm run version`：套用待處理的 changesets，更新 `package.json` 版本與 `CHANGELOG.md`。
- `scripts/link-skills.sh`：將 `skills/` 下的 skills symlink 到 `~/.Codex/skills` 與 `~/.agents/skills`，供本機測試。這是開發用途，不是支援的 installer；新增、移除或重新命名 skill 後重新執行。

### Skills 結構與跨檔案規則

來源 skills 依 `skills/<bucket>/<skill-name>/SKILL.md` 組織，bucket 包含：

- `engineering/`：日常程式碼工作
- `productivity/`：日常非程式碼工作流程工具
- `misc/`：保留但很少使用，不列入推廣
- `personal/`：綁定個人環境設定，不列入推廣
- `in-progress/`：尚未準備好發布的草稿
- `deprecated/`：不再使用的 skills

`engineering/` 與 `productivity/` 是 promoted buckets。每個 promoted skill 都要同步登錄在根目錄 `README.md` 與 `.claude-plugin/plugin.json`。非 promoted bucket 的 skills 不得登錄在這兩個 registry。

每個 bucket 都有 `skills/<bucket>/README.md`，列出該 bucket 的每個 skill，名稱連到對應的 `SKILL.md`。Promoted bucket 的 README 與根目錄 README 另外分成 User-invoked 與 Model-invoked；非 promoted bucket 使用平面清單。

目前 repo root-level 的 `skills/base/`、`skills/code-review/`、`skills/code-review-ocr/`、`skills/implement/` 與 `skills/wait-what/` 以各自的 `SKILL.md` 作為唯一 canonical 文件，內容以台灣正體中文為主；不另維護 `SKILL.zh-TW.md` sibling file。

### Invocation mode

請參考 `.agents/invocation.md`。每個 `SKILL.md` 都屬於以下其中一種：

- User-invoked：frontmatter 有 `disable-model-invocation: true`，使用者輸入 skill 名稱才能觸發。
- Model-invoked：frontmatter 不含該欄位，使用面向模型且包含觸發條件的 description，符合情境時由 agent 自動觸發。

User-invoked skill 可以在自身 prose 中呼叫 Model-invoked skill，不得呼叫另一個 User-invoked skill。User-invoked skill 沒有可供模型自動觸發的 description。

### Docs pages

`engineering/` 或 `productivity/` 下的 skills 也要有 `docs/<bucket>/<skill-name>.md` 人類閱讀頁面。頁面會發布到 repo 設定的 docs domain，URL 格式固定為 `<docs-domain>/skills-<skill-name>`。第一次發布頁面前，先在 `.agents/writing-docs.md` 選定並記錄 domain，再依其中的 template 與慣例建立或同步頁面。

Promoted skill 新增、重新命名或行為變更時，建立或同步對應頁面。Skill 移入或移出 promoted bucket 時，新增或移除頁面；重新命名時，移動 `docs/<bucket>/<old>.md` 到 `docs/<bucket>/<new>.md`。Docs page 的連結都必須使用 absolute URL，因為頁面會在 repo 外部發布。

### Domain vocabulary

使用 `CONTEXT.md` 定義的 canonical terms，編輯觸及相同 domain concept 的 skills 時，優先使用這些詞彙。這個 glossary 會延遲建立，直到 `/domain-modeling` skill 首次確定一個 term。

## 程式碼搜尋與導覽

以下規則涵蓋精確搜尋、結構查詢、MCP 圖譜證據驗證與降級處理。

<!-- codebase-memory-mcp:start -->
# Codebase Memory 使用規則

## 程式碼知識圖譜（codebase-memory-mcp）

本專案使用 `codebase-memory-mcp` 維護程式碼知識圖譜。依查詢類型選擇工具：精確文字與檔案搜尋使用 `rg` / `rg --files`；結構與關聯查詢優先使用 MCP 圖譜工具。若工具不存在、索引無法建立或結果不足，才改用其他唯讀工具，並標示降級限制。

### 查詢類型

#### 精確文字與檔案搜尋

適用於已知函式名稱、變數、字串常值、錯誤訊息、設定值或檔案路徑。

- 內容搜尋使用 `rg`。
- 檔案搜尋使用 `rg --files`。
- 非程式碼檔案，例如 Dockerfile、shell script 與設定檔，也使用文字搜尋工具。
- 只有在搜尋範圍很大且需要獨立調查時，才委派探索代理。

#### 結構與關聯查詢

適用於呼叫者、被呼叫者、相依關係、匯入圖、呼叫鏈與變更影響分析。

- 這類查詢優先使用 MCP 圖譜工具，不要只靠文字搜尋回答「誰呼叫某函式」或「修改某符號會影響什麼」等問題。
- 查詢前先確認最近的圖譜專案與索引版本。索引不存在或過期時，執行一次 `index_repository`，再重試。
- MCP 工具不可用、索引無法建立或結果不足時，才改用語言伺服器、建置工具或文字搜尋近似分析，並明確標示限制。

### MCP 圖譜查詢順序

完成索引確認後，依序使用下列工具：

1. `search_graph`：依 pattern 尋找函式、class、route 與變數。
2. `trace_path`：追蹤函式的呼叫者或被呼叫者。
3. `get_code_snippet`：讀取指定函式或 class 的原始碼。
4. `check_index_coverage`：在提出結論前，驗證候選路徑與索引遺漏範圍。
5. `query_graph`：執行複雜的 Cypher 查詢。
6. `get_architecture`：取得專案的高階架構摘要。

### 證據層級

- **探索層（Scout，Tier 1）**：用少量查詢與指定原始碼做快速正向確認。結果只能視為暫定，不得據此提出否定或完整性結論。
- **驗證層（Verify，Tier 2，預設）**：依任務取得圖譜證據、相關方向的呼叫鏈、支撐重要結論的原始碼片段與完整分頁結果。
- **稽核層（Auditor，Tier 3）**：在限定範圍內使用目前索引版本做完整驗證，涵蓋所有相關分頁、呼叫雙向與重要關聯，並揭露限制。
- 任何證據層級取得圖譜候選後，對所有證據路徑呼叫一次 `check_index_coverage`。若要提出否定或完整性結論，也要加入相應範圍。結果乾淨只代表沒有記錄到缺口，不代表內容絕對完整。若涵蓋範圍是 partial、skipped、excluded、stale、pending 或 unknown，先讀取或搜尋回報的範圍，再依圖譜結果下結論。

### 改用 grep/glob 的時機

- 搜尋字串常值、錯誤訊息、設定值或明確檔案路徑。
- 搜尋非程式碼檔案，例如 Dockerfile、shell script 與設定檔。
- MCP 工具不可用、索引無法準備或圖譜結果不足時。

### 範例

- 尋找 handler：`search_graph(name_pattern=".*OrderHandler.*")`
- 查詢呼叫者：`trace_path(function_name="OrderHandler", direction="inbound")`
- 讀取原始碼：`get_code_snippet(qualified_name="pkg/orders.OrderHandler")`

### 工作階段重設與子代理

- 在工作階段開始或內容壓縮後，用 `list_projects` 或 `index_status` 確認最近的圖譜專案與索引版本，再選擇探索、驗證或稽核層級。
- 建立子代理前，先在父工作階段查詢圖譜與涵蓋範圍。交付內容要包含證據層級、專案、索引版本／新鮮度、限定範圍、查詢與分頁狀態、完整符號名稱、路徑、呼叫鏈結果、含範圍與理由的涵蓋範圍證據、已執行的文字搜尋降級處理，以及尚未解決的問題。
- 不要假設子代理會繼承 MCP 存取權或父工作階段內容。子代理沒有 MCP 工具時，不得聲稱已使用 MCP；應使用交付的證據讀取或搜尋指定原始碼，尤其是每個回報的索引遺漏範圍。
<!-- codebase-memory-mcp:end -->
