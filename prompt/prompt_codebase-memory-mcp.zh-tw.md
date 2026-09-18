# 程式碼搜尋與導覽

- 精確文字、符號名稱、路徑、錯誤訊息、設定與非程式碼檔案使用 `rg` / `rg --files`。只有大範圍且可獨立調查時才委派探索。
- 結構、呼叫者／被呼叫者、相依、呼叫鏈與變更影響優先使用 codebase-memory-mcp。工作階段開始、compaction 後及結構查詢前，用 `list_projects` 或 `index_status` 確認專案與索引版本；不存在或過期時 `index_repository` 一次再重試。
- 依序用 `search_graph` 找符號、`trace_path` 查關聯、`get_code_snippet` 讀原始碼、`check_index_coverage` 驗證涵蓋範圍；複雜查詢用 `query_graph`，高階摘要用 `get_architecture`。檢查並讀完所需分頁。
- 預設 Verify：結論須有圖譜、相關方向呼叫鏈、原始碼與完整分頁證據。Scout 僅暫定正向確認；Auditor 在限定範圍核對雙向關聯與完整證據。
- 所有圖譜證據路徑都查 `check_index_coverage`；否定或完整性結論另查相應範圍。遇 partial、skipped、excluded、stale、pending 或 unknown，先補讀／搜尋缺口。無記錄缺口也不保證完整。
- MCP 不可用、索引無法建立或結果不足時，才改用語言伺服器、建置工具或文字搜尋，明示降級限制。
- 委派前由父代理確認圖譜與涵蓋範圍；交付證據層級、專案／索引版本、範圍、分頁狀態、完整符號與路徑、呼叫鏈、缺口與補查、未解問題。子代理不得假設有 MCP 存取權；無工具時用交付證據補讀原始碼並揭露限制。
