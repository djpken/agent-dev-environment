# Agent instructions

本 repo 是 Agent Development Environment monorepo。`CLAUDE.md` 引用本檔。

## 職責與驗證

- `ade/` 擁有 runtime、安裝、設定、provider 與 host 整合；`skills/`、`prompt/` 擁有 Workflow Pack。
- Runtime 與 adapter 管理版本、credentials、process lifecycle 與重連；provider 擁有業務狀態，adapter 只保存整合狀態。Workflow Pack 宣告能力與使用政策。
- 調整職責前讀 `docs/ade-responsibilities.md`。修改 runtime、安裝流程或 host adapter 時，讀 `docs/ade-runtime.md` 並執行 `uv run --frozen python -m unittest discover -s tests -v`。
- Python runtime 使用 `uv.lock`；npm 只用於 changesets。準備發布變更用 `npx changeset`；版本更新用 `npm run version`。

## 查詢與輸出效率

- 先縮小檔案與行號範圍，再讀必要內容；同一份未變資料不重讀。獨立查詢合併執行，有依賴的操作依序處理。
- 大量 log／JSON 先用搜尋或程式篩選目標與摘要；保留完整錯誤、證據位置及原始資料路徑供補查。結果截斷時縮小查詢或分頁，不能把截斷結果當成完整證據。

## 按任務讀取

- Linear issue、spec 或狀態操作：讀 `docs/agents/issue-tracker.md`，使用 `orca-linear` skill；triage 另讀 `docs/agents/triage-labels.md`。
- 編輯 skills、prompts、registry 或相關文件：先讀 `docs/agents/workflow-pack.md`；prompts 另讀 `prompt/README.md`。遵守 invocation、bucket、registry、docs page 與來源追蹤規則。
- Domain 詞彙使用根目錄 `CONTEXT.md`。修改 domain docs 或 ADR 時讀 `docs/agents/domain.md`。

## 程式碼搜尋與導覽

<!-- codebase-memory-mcp:start -->
- 精確文字、檔案路徑與非程式碼檔案：用 `rg` / `rg --files`。
- 工作階段開始或 compaction 後，用 `list_projects` 或 `index_status` 確認圖譜專案與版本。
- 結構、呼叫鏈、相依或影響分析，以及委派程式碼探索前：先讀 `docs/agents/code-search.md`。優先用 codebase-memory-mcp，預設 Verify 層級；索引不存在或過期時建立一次再重試。
- 圖譜候選的所有證據路徑須查 `check_index_coverage`，完整性結論另查限定範圍與全部分頁。補讀缺口；無缺口不代表絕對完整。
- 工具不可用、索引無法建立或結果不足時，才改用其他唯讀工具並標示降級限制。子代理交付須含證據、涵蓋範圍與未解問題，不假設它能存取 MCP。
<!-- codebase-memory-mcp:end -->
