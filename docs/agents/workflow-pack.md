# Workflow Pack 維護規則

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
