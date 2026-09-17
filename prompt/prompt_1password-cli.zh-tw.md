# 1Password CLI

- 需要專案密鑰、API token 或環境變數時，優先使用 PATH 中的 `op` CLI 與既有驗證設定。
- 先用 `op --version` 確認工具可用，再用 `op whoami` 驗證登入；驗證失敗時回報錯誤並請使用者修復驗證，不要求把 token 貼到對話中。
- 需要尋找項目時，使用 `op vault list` 與 `op item list --vault <vault>`，只查詢任務所需範圍。
- 執行需要密鑰的程式時，優先用 `op run --env-file <references-file> -- <command>`，檔案只放 `op://` references，密鑰由 CLI 注入子程序。保留預設輸出遮罩，避免子程序列印密鑰或完整環境變數。
- 直接呼叫 `op`，保留環境既有的 wrapper；不要讀取、複製或修改 CLI 的 token 檔案，也不要把密鑰或 Service Account token 寫入對話、log、repo 或文件。
- 未經使用者要求，不新增、修改或刪除 1Password 項目；既有授權範圍內的唯讀查詢可直接執行。
