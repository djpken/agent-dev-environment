# ADR-0008：以 agent-dev-environment monorepo 維護 ADE

- 狀態：接受

將原本 `plugins-zh-tw` 與新建立的 ADE runtime 合併，repository 更名為 `agent-dev-environment`。個人開發者需要同時調整 skills、MCP 使用契約與 adapters；同 repo 可用同一個 commit 與測試驗證，省去跨 repo 版本搭配成本。此決策取代 ADR-0004 的 repository 分離選擇，保留個人使用者優先與共享核心加 Host adapters 的方向。

`skills/`、`prompt/` 擁有 Workflow Pack；`ade/` 擁有 composition runtime，`ade/hosts.py` 擁有 host 設定轉換，`ade.lock.json` 與 provider schema 擁有外部能力的宣告。外部 OCR 與 Headroom 原始碼維持上游獨立。Plugin manifest 的 `skills` 名稱與既有 skill invocation keys 維持相容，可只安裝 Workflow Pack，也可使用完整 ADE CLI。

Workflow 預設從目前 monorepo 的 `skills/`、`prompt/`、plugin manifest 與 LICENSE 建立 bundle。Plan ID 包含 bundle 內容與 executable bits 的 digest；plan 後內容改變會拒絕 apply。Generation 保存實際 bundle 與 digest，使 rollback 不依賴工作目錄的後續修改。Runtime、tests、credentials 與其他 repo 檔案不混入 Workflow Pack。

安裝、設定與 process lifecycle 歸 runtime；技能文件可以說明 MCP 使用方法。Provider 業務狀態與有狀態的 review 政策不能因方便轉接就塞進 adapter。舊 OCR variant 的相容性遷移仍依 ADR-0002 與 ADR-0006 的邊界處理。
