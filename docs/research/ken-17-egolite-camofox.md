# KEN-17：ego-lite 與 camofox-browser 比較

- **查閱日：** 2026-09-24
- **EgoLite 參照：** GitHub 搜尋 `egolite` 後確認為 [citrolabs/ego-lite](https://github.com/citrolabs/ego-lite)
- **範圍：** 比較兩者的瀏覽器形態、agent 存取方式、登入狀態與預期用途。未執行實際效能或網站相容性測試。

## 差異

| 面向 | ego-lite | camofox-browser |
| --- | --- | --- |
| 核心定位 | 面向人與 agent 共用的桌面瀏覽器；agent 在自己的 Space 操作，沿用使用者的登入狀態，不搶佔使用者的瀏覽視窗。 | 面向 agent 的 headless browser server，以 Camoufox Firefox fork 做 anti-detection，透過 REST API 提供瀏覽操作。 |
| Agent 介面 | `ego-browser` CLI / Skill 執行 JavaScript 瀏覽腳本，使用 snapshot、element refs 和 CDP helper；可管理多個 task spaces。 | REST API；另有 stdio MCP adapter 將相同 browser 工具轉接給 MCP host。支援 session isolation、cookie import、proxy/GeoIP 和 accessibility snapshot。 |
| 登入與隔離 | browser profile 保存 cookie 和登入狀態；agent Space 與使用者並行，預設可重用使用者登入狀態。 | 每個 session 分開保存狀態；可匯入 Netscape cookie，也可設定 proxy。並非以共用使用者目前桌面視窗為主要模式。 |
| 執行與散布 | 官方 README 說明目前桌面 app 支援 macOS，Windows 為 closed beta，Linux 在 roadmap。GitHub repo 提供開源 SDK/Skill；瀏覽器 app 與 native bindings 由已安裝 app 提供。 | Node.js server，使用 Camoufox/Playwright 依賴，可在本機或遠端主機提供 HTTP 服務；MCP adapter 可獨立安裝。 |
| 適合情境 | 需要 agent 操作使用者已登入的 SaaS、內部工具或一般網站，同時讓使用者繼續使用自己的瀏覽器。 | 需要 headless、可服務化的 agent browser，並且工作需要專案主打的 anti-detection、cookie/session 或 proxy 能力。 |

來源：[ego-lite GitHub README](https://github.com/citrolabs/ego-lite)、[ego-browser Skill](https://github.com/citrolabs/ego-lite/blob/main/skills/ego-browser/SKILL.md)、[ego-lite architecture](https://github.com/citrolabs/ego-lite/wiki/Ego-Browser-CLI-Architecture)、[camofox-browser README](https://github.com/jo-inc/camofox-browser/blob/master/README.md)、[camofox MCP README](https://github.com/jo-inc/camofox-browser/blob/master/mcp/README.md)。

## ADE 選型

兩者解決不同需求，不能只按「哪個比較快」排出通用優勝者。ADE 若要提供登入後的人機共用瀏覽流程，ego-lite 的 profile 與 task-space 模型較相符，但其桌面 app 目前平台限制會影響 ADE runtime 部署。若需要 headless provider 或遠端 server，camofox-browser 的服務介面較直接；需另評估其 anti-detection、cookie import、proxy 與外部網路權限，並固定 browser binary 和 Node package 版本。

目前不新增預設 provider。此文件完成定位比較，實際採用前仍需先選定代表性網頁任務，再量測登入流程、操作成功率、隔離、相容性和資源使用。README 宣稱的 stealth、速度和 token 數據尚未由 ADE 環境獨立驗證。
