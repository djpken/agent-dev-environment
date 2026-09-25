# agent-dev-environment

繁體中文 · [English](./README.en.md) · [README.zh-TW.md](./README.zh-TW.md)

個人開發者的 local-first Agent Development Environment。此 monorepo 同時維護 skills、個人 prompts、provider 管理與 Claude Code／Codex／OpenCode 的 host adapters。

## 兩個使用入口

只使用 skills：安裝現有 `.claude-plugin/plugin.json` 宣告的 Workflow Pack，plugin 名稱維持 `skills`，既有 slash commands 不變。

使用完整 ADE：先建置 Go CLI，再將 `plan` 輸出的 ID 帶入 `apply`。

```bash
mkdir -p .ade/bin
go build -o .ade/bin/ade ./cmd/ade
.ade/bin/ade plan --user user.example.json
.ade/bin/ade apply --user user.example.json --plan-id <plan_id>
.ade/bin/ade doctor
```

使用與限制見 [ADE runtime](./docs/ade-runtime.md)，其中包含 `orca serve` 與 `Local` execution profiles 的說明；SSH MCP 的 user-level 設定見 [SSH MCP 文件](./docs/ssh-mcp.md)；Plane 到 Linear 同步見 [同步文件](./docs/ade-plane-linear-sync.md)，職責分工見 [責任表](./docs/ade-responsibilities.md)。`user.example.json` 未設定 LLM endpoint，因此模型 review 會保持 blocked；credentials 不寫入 repository。

## 模組邊界

| 位置 | 責任 |
| --- | --- |
| `skills/`、`prompt/` | 工作方法、MCP 使用政策、結果處理 |
| `cmd/ade/` | Go CLI 與 process lifecycle |
| `internal/ade/` | Go composition/runtime、host adapters、sync、scheduler、ADES API 與 wallet controls |
| `deploy/agent-environment/` | Go binary 安裝、Linux + systemd VM recipe 與 root-owned helpers |
| `ade.lock.json`、`ade/provider.schema.json` | Provider 版本、能力、權限與健康檢查契約 |
| `tests/` | Runtime 與 host 整合驗證 |

`npm` 建置 React dashboard 並管理 changesets；Go runtime 依 `go.mod` 管理相依套件。Python ADE modules 僅保留給既有行為測試；CLI、runtime、部署與 ADES API 都使用 Go。

```bash
go build ./...
```

## 目錄結構

Skills 放在 `skills/<bucket>/<skill-name>/SKILL.md` 下。Bucket 分類：

- `engineering/`：日常程式碼工作
- `productivity/`：日常非程式碼工作流程工具
- `misc/`：保留但很少用，不列入推廣
- `personal/`：綁定個人環境設定，不列入推廣
- `in-progress/`：草稿，尚未準備好發布
- `deprecated/`：已不再使用

`engineering/` 與 `productivity/` 的 promoted skills 會登錄在這份 README 與 `.claude-plugin/plugin.json`。

## 本機開發

```bash
scripts/link-skills.sh
```

把 `skills/` 底下每個 skill symlink 到 `~/.claude/skills` 與 `~/.agents/skills`，供本機測試。新增、移除或重新命名 skill 後要重新執行。

## User-invoked skills

- [`/git-fork-remotes`](./skills/engineering/git-fork-remotes/SKILL.md)：Fork repo 後設定 fork/upstream remote，並修正 branch tracking。
- [`/translating-skill-docs`](./skills/productivity/translating-skill-docs/SKILL.md)：為每個 skill 加上翻譯後的 `SKILL.<locale>.md` sibling file，不改變 runtime 行為。
- [`/writing-router-skill`](./skills/engineering/writing-router-skill/SKILL.md)：判斷目前情境適合使用哪個 skill。

## Model-invoked skills

- [`/hv-analysis`](./skills/productivity/hv-analysis/SKILL.md)：執行橫縱分析法並產出 PDF 報告。
- [`/storage-analyzer`](./skills/productivity/storage-analyzer/SKILL.md)：唯讀分析 macOS/Windows 儲存空間並產出互動式 HTML 報告。
- [`/neat-freak`](./skills/engineering/neat-freak/SKILL.md)：整理專案文件、規則檔、agent memory 與 workspace 殘留物。

## 既有 plugin 與 prompt

- `prompt/`：獨立 prompt 收藏，收錄從各 skill / plugin repo 匯出的 standalone prompt 文字檔。
- `skills/base/`、`skills/code-review/`、`skills/code-review-ocr/`、`skills/implement/`、`skills/wait-what/`：root-level skills，唯一的 `SKILL.md` 以台灣正體中文為主，不另維護 `SKILL.zh-TW.md` sibling file。
