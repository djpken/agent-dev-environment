# Skills 與 MCP 更新檢查

ADES 登入後可由「手動檢查更新」盤點目前 ADE generation 的 Skills 與 MCP providers。檢查只讀取本機安裝內容並向 GitHub API 查詢來源 repo 的 default branch，不會修改 generation、source checkout 或 provider 設定，也不會自動安裝更新。

來源 repo 取自 `source_root/ade.lock.json` 的 `workflow.repository`，目前只支援 HTTPS GitHub repository。Skills 逐檔比對安裝 generation 的 `workflow/skills/` 與 GitHub default branch；結果包括已同步、有更新、尚未安裝與上游已移除。MCP provider 從安裝設定中讀取 `stdio`、`http` transport，並將安裝版本與 GitHub `ade.lock.json` 的鎖定版本比較。

盤點範圍只含 ADE generation 管理的 Workflow Pack Skills 與 MCP providers，不讀取 Claude Code、Codex 或 OpenCode 自己的設定檔，也不會發現 ADE 未管理的 entries。

MCP provider 的狀態包含是否已啟用、generation 鎖定版本，以及是否有新版鎖定版本。ADES 不會啟動 stdio server 或探測 host 的 MCP session；連線狀態回報為未檢查。外部 provider 的最新版本以 repository 鎖定清單為準；ADES 不會直接查詢 npm、PyPI 或其他 package registry。

手動檢查由已登入的 viewer 透過 `POST /api/v1/agent-assets/check` 觸發，查詢逾時為 30 秒。ADE 安裝根目錄預設為服務使用者的 `~/.local/share/ade`；可在 ADES 設定中用絕對路徑 `ade_root` 覆寫。
