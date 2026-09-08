# ADR-0005：採用受控 provider 擴充與 local-first 資料邊界

- 狀態：接受
- 日期：2026-09-04

ADE 內建少量且版本固定的 Capability providers，並允許第三方 provider 透過 Provider manifest 宣告 schema、health check 與 permissions 後加入；不自動載入環境中找到的任意 tools 或 MCP servers。原始碼、tool output 與 session state 預設留在本機，只能傳送到使用者明確設定的 LLM endpoint。這項政策保留個人環境的可擴充性，同時限制供應鏈、權限與資料外洩風險。
