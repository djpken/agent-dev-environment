# ADES 前後端技術選型研究備忘錄

- **狀態：** 技術選型已採用，仍不是正式 ADR
- **資料查閱日：** 2026-09-24
- **實作狀態（2026-09-24）：** React/Vite 與 NestJS on Express 提供正式 dashboard/API。NestJS controllers/providers 組織路由與管理操作，Express 負責 HTTP transport、middleware 與靜態檔案。TypeScript service 已直接處理 health probes、run state、trending 與 artifact inventory/upload/delete，不再為這些管理操作啟動 Python CLI 子程序。舊 Python inline dashboard 已移除，legacy Python service 的 `GET /` 回傳 `410`；Python JSON API、Python CLI/runtime 與 root-owned helpers 保留。下方「變更前」程式碼連結固定指向採用前版本 `eef08709c3741f700d352ae6e1b9e5640283697a`。
- **範圍：** ADES per-VM 管理服務的瀏覽器 dashboard、API 與部署。`ade publish-html` 的作品 publisher 使用獨立 HTTP origin，不列入此 dashboard 的前端選型。

## 結論摘要

- 前端採用 React、TypeScript 與 Vite；Node.js、TypeScript、NestJS 與 Express adapter 接管 ADES 對外 HTTP/API、wallet routes 與靜態頁面。兩端都以 TypeScript 開發。
- NestJS controllers、providers、guards、validation pipes 與 exception filter 組織 HTTP 應用層；Express 提供 NestJS 的 HTTP adapter，維持既有 `/api/v1/...` API 格式。health probes、run state、trending parser 與 dashboard artifact I/O 已移到 TypeScript；root-owned authorization、policy verification 與 update helpers 保持 Python 特權邊界。
- 這項採用替換管理頁面與 HTTP adapter，沒有移除 ADE Python CLI/runtime。VM 同時維護 Node.js 與 Python；Node build 依 `web/package-lock.json` 鎖定，服務以 `node --jitless` 遵守 systemd 的 `MemoryDenyWriteExecute=true`。
- NestJS 加 Express adapter 提供明確的模組、依賴注入與 request validation 邊界。代價是增加 framework abstraction、decorator/DI 慣例與相依套件；目前 ADES 管理功能持續擴張，因此採用這層應用架構。Go、Spring Boot 與 Rust/Axum 仍是可行選項，但本次決策不採用。
- 後續可另案評估將 Python domain 邏輯改成 TypeScript。那會擴大遷移範圍，需處理 CLI、持久狀態、wallet 驗證與 root helper 交界，不納入目前 HTTP adapter 替換。

## 採用決策 2026-09-23，架構調整 2026-09-24

初始實作採用 **React + TypeScript + Vite** 前端，以及 **Node.js + TypeScript + Fastify** 管理 HTTP/API service。管理功能擴大後，先將應用層改為 NestJS；本次再改用 **NestJS + Express adapter** 並移除 Fastify，讓 NestJS 使用預設 HTTP provider，減少 ADES 不需要的 adapter 與 plugin 相依。NestJS 負責 modules、controllers、dependency injection、DTO validation 與 session guard；Express 提供 HTTP server、middleware 和靜態檔案服務。代價是要重寫 Fastify hooks 與型別整合，並持續遵守 NestJS 的裝飾器和 DI 慣例。

同 origin 與既有 `/api/v1/...` contract 保持不變。TypeScript service 直接處理 Web 管理 API 使用的 probes、runs、trending 與 artifacts，Python CLI/runtime 及 root-owned authorization/update helpers 保留。部署使用 Node.js `22.12+`、npm lock install 和 production build，Node 以 `--jitless` 配合 systemd `MemoryDenyWriteExecute=true`。

這是採用技術組合與 HTTP adapter 的工作決策，尚未建立正式 ADR。移除 Python runtime 屬於另一個遷移目標，不包含在這次修改。

## ADES 變更前的現況與限制

- `dashboard_html()` 在 [`ade/environment.py`](https://github.com/djpken/agent-dev-environment/blob/eef08709c3741f700d352ae6e1b9e5640283697a/ade/environment.py#L1087) 回傳包含 inline CSS、markup 與 JavaScript 的完整文件。`GET /` 直接回傳這個 HTML；同一個 handler 也負責公開 trending 與管理 API 路由，見 [`EnvironmentHTTPServer.handle`](https://github.com/djpken/agent-dev-environment/blob/eef08709c3741f700d352ae6e1b9e5640283697a/ade/environment.py#L1745)。
- 前端包含公開排行、wallet 首次註冊、EIP-191 簽章登入、session 復原與登出、角色限制、元件更新、排程設定、執行紀錄及 HTML bundle 上傳。共用狀態和事件處理集中在頁面 script，例如 [`environment.py`](https://github.com/djpken/agent-dev-environment/blob/eef08709c3741f700d352ae6e1b9e5640283697a/ade/environment.py#L1129)、[`environment.py`](https://github.com/djpken/agent-dev-environment/blob/eef08709c3741f700d352ae6e1b9e5640283697a/ade/environment.py#L1212)、[`environment.py`](https://github.com/djpken/agent-dev-environment/blob/eef08709c3741f700d352ae6e1b9e5640283697a/ade/environment.py#L1315) 與 [`environment.py`](https://github.com/djpken/agent-dev-environment/blob/eef08709c3741f700d352ae6e1b9e5640283697a/ade/environment.py#L1356)。
- 部署服務以非 root 帳號執行；root-owned helper 負責授權與更新操作。systemd 透過 `uv run --project "$ADE_SOURCE_ROOT"` 啟動 Python CLI，見 [`agent-environment.service`](../../deploy/agent-environment/agent-environment.service#L6) 和 [`ade-environment-service.wrapper`](../../deploy/agent-environment/ade-environment-service.wrapper#L1)。核心邏輯會呼叫 sudo helper 和系統程序，見 [`environment.py`](https://github.com/djpken/agent-dev-environment/blob/eef08709c3741f700d352ae6e1b9e5640283697a/ade/environment.py#L1655) 與 [`environment.py`](https://github.com/djpken/agent-dev-environment/blob/eef08709c3741f700d352ae6e1b9e5640283697a/ade/environment.py#L1716)。這些責任目前與 Python runtime 緊密相連。
- Nginx 在 443 提供管理 dashboard/API 並代理至服務的 6790 port；HTML artifact 維持 HTTP 80 的獨立 publisher origin。部署文件明確要求兩個 origin 分離，見 [`deploy/agent-environment/README.md`](../../deploy/agent-environment/README.md#L58) 和 [artifact publishing 說明](../../deploy/agent-environment/README.md#L89)。新 bundle 應由管理服務同 origin 提供，不能把管理 API 或登入資訊移到 artifact origin。
- repo 目前以 Python 與 `uv.lock` 管理 runtime，`npm` 用於 changesets；`pyproject.toml` 的 package data 目前只列 JSON。見 [`README.zh-TW.md`](../../README.zh-TW.md#L22) 與 [`pyproject.toml`](../../pyproject.toml#L5)。加入 Vite 會新增前端 build/package 流程，但不必讓 VM 執行 Node.js。

## 選項比較

| 選項 | 適用情境 | 主要代價 | 對 ADES 的判斷 |
| --- | --- | --- | --- |
| 維持 inline HTML/CSS/JavaScript | 畫面少、互動簡單、低頻修改，或只需短期維持現況 | 模板字串、狀態、DOM 更新與 API 呼叫繼續集中；模組邊界和編譯期檢查不足 | 暫時可運作，但目前頁面已跨多個管理流程，不建議再把新面板堆入同一函式 |
| React + Vite + TypeScript，Python API | 團隊熟悉 React，或需要 React 生態系元件與後續擴充 | HTML 需轉成 JSX/元件；路由、資料載入與狀態管理需自行選擇。React 官方說明可用 Vite 建立 client-only SPA，也提醒需自行處理常見 app pattern | 合適。Vite 的 backend integration 明確涵蓋傳統 backend，可將 build 產物接回 Python handler；部署期間不需 Node runtime |
| Vue + Vite + TypeScript，Python API | 團隊已有 Vue 經驗，或特別偏好 Vue SFC 的 template 結構 | 需建立獨立 npm build/type-check 流程；Vite 轉譯 TypeScript 不等於型別檢查，需另外執行 `vue-tsc` 或 TypeScript checker | 技術上可行；本次前端決策採 React + TypeScript，因此不列為首選 |
| Python API 加 production ASGI server，例如 FastAPI 或 Starlette + Uvicorn | 保留 ADE 的 Python domain/service code，同時替換標準庫 HTTP layer | 將 handler 移成 ASGI routes/middleware，核對同步 I/O、狀態與程序操作；增加 Python web/server dependencies | 應與前端拆分並行評估。FastAPI 依型別產生 OpenAPI/JSON Schema；Starlette 提供較精簡的 ASGI routing、middleware 與 static files，可改善 HTTP adapter 邊界並保留 wallet 和 OS helper 邏輯 |
| Node.js + TypeScript，Fastify routes/plugins | 團隊希望前後端共用語言與 schema，服務邊界精簡，且願意自行建立模組慣例 | 需自行組合依賴注入、驗證、授權及錯誤處理慣例；仍需處理 wallet、session、檔案狀態、subprocess、sudo helper 與 systemd 整合；VM 增加 Node runtime 與套件維護 | HTTP runtime 輕、plugin encapsulation 明確，適合 route 數量有限且團隊想掌握組裝方式的服務。管理功能持續擴張時，須自行維持跨模組一致性 |
| Node.js + TypeScript，NestJS on Express | 管理 API 持續增加，團隊需要明確的 modules/controllers/providers、DI 與共用 request validation | 增加 NestJS abstraction、decorator/DI 慣例、相依套件和學習成本；仍需保留 Python runtime/root helper 的程序邊界 | 適合目前 ADES 管理面的成長方向。NestJS 提供應用層結構，Express 使用 NestJS 預設 HTTP provider；能集中 session guard、DTO validation 和錯誤轉換。需避免為了 framework 將小型 service 過度拆分 |
| Go，net/http 或輕量 router | 希望服務以編譯後的執行檔部署，並重視明確的併發與系統程序控制 | 團隊需學 Go；React 型別無法直接共用，需以 OpenAPI/JSON Schema 維持 API 契約；仍須重做 Python domain 整合 | 適合把管理服務做成獨立 daemon。Go 標準庫提供 HTTP server 和 os/exec；os/exec 預設不經 shell。CI 可編譯目標 Linux 架構的執行檔，VM 不必安裝 Go toolchain |
| Java/Kotlin，Spring Boot | 組織已有 JVM 維運、安全與監控標準，或預期服務會長成較大型的 API | 增加 JVM 和 Spring 生態的建置、升級、設定與維運；現有 Python domain 與 helper 仍需遷移或透過程序介接 | Spring Boot 提供 production service 常見的 embedded server、security、metrics、health checks 與外部設定；若團隊沒有 JVM 經驗，對目前單一管理服務會增加學習與維運面積 |
| Rust，Axum | 已有 Rust 經驗，且具體需要 Rust 的型別/記憶體安全或低階資源控制 | 語言與生態學習成本最高；前端型別不能直接共用；Python 業務邏輯和系統整合都需重做 | Axum 提供 HTTP routing 與 request handling；Rust 的 std::process::Command 能直接設定程序與參數。對目前以 API、排程和 OS helper 為主的服務，效能本身不足以抵銷遷移成本 |
| Next.js SSR | 公開、多路由、重視搜尋索引或需要在 server render 前取資料的網站；或產品確定需要 React Server Components 等 server features | 正式 Node server、Node 更新與 Next 部署設定；若只用 static export，Node server features 不可用，採用 SSR 則需要額外服務 runtime | 現有單一管理頁面以 wallet/session 後的瀏覽器互動為主，搜尋索引與 server-side 個人化效益有限。現在導入 SSR 不值得 |

React 官方文件將「內部 admin tool」列為可以由 build tool 起步的 SPA 情境，也說明需要 routing 時可考慮 framework。React + Vite 是受官方文件支援的路徑；Vue 官方文件則建議新專案使用 `create-vue` 與 Vite，並提供 Vue SFC 的 TypeScript 支援。[React build tool 與 SPA 說明](https://react.dev/learn/build-a-react-app-from-scratch)、[React 建立 App 的選擇](https://react.dev/learn/creating-a-react-app)、[Vue tooling](https://vuejs.org/guide/scaling-up/tooling)、[Vue TypeScript 支援](https://vuejs.org/guide/typescript/overview)。

Vite 的 production build 輸出可由靜態主機提供；它也有將 manifest、CSS 和 module preload 接入傳統 backend 的整合指南。對 ADES，可在開發與 CI 用 Node 建置，再將靜態結果隨 ADE source/package 部署，由 Python 在 `/` 和同 origin 的 assets path 提供。Vite 預設不執行 TypeScript type-check，production build 應另跑 `tsc --noEmit`，Vue SFC 專案則用 `vue-tsc`。[Vite build](https://vite.dev/guide/build)、[Vite backend integration](https://vite.dev/guide/backend-integration)、[Vite TypeScript 行為](https://vite.dev/guide/features)。

## 後端候選比較（採用前）

**Node.js + TypeScript + Fastify** 最接近 React 前端：可以用 TypeScript、JSON Schema/TypeBox 或 OpenAPI 型別生成維持 API 型別。Fastify 會依 JSON Schema 驗證 request 與序列化 response；Node 的 `execFile()` 可用 argv 啟動 helper，預設不經 shell。[Fastify TypeScript](https://fastify.dev/docs/latest/Reference/TypeScript/)、[Fastify validation/serialization](https://fastify.dev/docs/latest/Reference/Validation-and-Serialization/)、[Node child_process](https://nodejs.org/api/child_process.html)。

這種共享語言主要改善前後端開發協作，不能省掉 runtime validation，也不會直接重用 ADE 的 Python domain code。若 Node service 透過 Python CLI 執行原有操作，就會同時部署兩個 runtime 和一段程序間契約；若要移除 Python，還得搬遷 CLI、domain、wallet 驗簽和 privileged helper 整合。

**Go** 是較務實的獨立 daemon 候選：標準庫 `net/http` 可實作 HTTP server，`go build` 產生目標平台的執行檔；`os/exec` 使用程式與參數執行子程序，不會預設呼叫 shell。[Go net/http server 範例](https://go.dev/doc/articles/wiki/)、[Go compile/install](https://go.dev/doc/tutorial/compile-install)、[Go os/exec](https://pkg.go.dev/os/exec)。代價是團隊要維護 Go，且不能直接共用 React 型別，需用 OpenAPI 或 JSON Schema 維持契約。編譯可以在 CI 做，VM 不必安裝 Go toolchain；build 仍要指定正確的 OS/architecture。

**Java/Kotlin + Spring Boot** 適合組織已有 JVM 技術標準的情況。Spring Boot 把 embedded server、security、metrics、health checks 和外部設定等常見 production 能力整合在框架中；代價是引入 JVM 與 Spring 的建置和維運流程，對目前單一管理服務增加一套平台。[Spring Boot](https://docs.spring.io/spring-boot/)。

**Rust + Axum** 適合已有 Rust 團隊，或有明確需要強型別約束、資源控制的情境。Rust ownership model 可在不依賴 garbage collector 的情況下提供 memory safety；Axum 是 HTTP routing/request-handling library，Rust `std::process::Command` 可直接設定程序與參數。[Rust ownership model](https://doc.rust-lang.org/book/ch04-00-understanding-ownership.html)、[Axum](https://docs.rs/axum/latest/axum/)、[Rust process Command](https://doc.rust-lang.org/std/process/struct.Command.html)。本服務主要處理 HTTP、檔案、排程與 OS helper；沒有已知的 CPU 熱點，因此單靠效能不足以支持採用 Rust。遷移與招募維護成本會是主要 trade-off。

以上選項都應維持同一份 API contract，並用固定 executable 加 argv 呼叫 helper。Node `execFile()`、Go `os/exec` 和 Rust `Command` 預設都能不經 shell 啟動程序；權限仍要留在既有 root-owned helper 與 sudo policy，換語言不會取代這條安全邊界。

## 原始條件式建議（採用前）

下列比較保留技術選型討論時的背景；其建議已由上方 2026-09-23 採用決策取代。

### 建議

把前端改為 React + Vite + TypeScript SPA，沿用既有 `/api/v1/...` 契約，維持同一個 HTTPS 管理 origin。由 Python backend 靜態提供 build 結果，build 與 type-check 在 CI 或 release 工作站執行。

後端第一階段保留 Python domain/service code，但不要把這項選擇等同於保留現有 `ThreadingHTTPServer`。Python 官方文件警告 `http.server` 不建議用於 production，且只實作基本安全檢查。先做 server hardening review，再比較 FastAPI + Uvicorn 與 Starlette + Uvicorn：需要 Pydantic request/response validation、OpenAPI/JSON Schema 契約時，優先試 FastAPI；需要精簡 ASGI routing、middleware 與 static files，並自行維持 API schema 時，試 Starlette。兩者都需保留 wallet 驗證、權限、持久狀態與 root-owned helper 邊界。[FastAPI request validation](https://fastapi.tiangolo.com/tutorial/body/)、[FastAPI OpenAPI](https://fastapi.tiangolo.com/tutorial/first-steps/)、[FastAPI deployment](https://fastapi.tiangolo.com/deployment/concepts/)、[FastAPI/Starlette 技術基礎](https://fastapi.tiangolo.com/)、[Starlette static files](https://www.starlette.io/staticfiles/)、[Python `http.server`](https://docs.python.org/3/library/http.server.html)。

若開始規劃移除 Python runtime，先做小型 POC 比較 NestJS on Express 與 Go。NestJS 適合優先驗證 modules/DI 與共享 TypeScript schema 是否減少跨層重工；Go 適合優先驗證獨立執行檔是否簡化 VM 升級與部署。Rust 或 Spring Boot 只在團隊已具備相應維護能力，或有明確需求時納入 POC。

只有在下列情況同時成立時，才啟動全 TypeScript backend 的正式評估：

1. 團隊有能力長期維護 Node.js 服務端與部署流程，且希望 Python 從 ADES runtime 移除。
2. 共享語言或端到端型別能解決已量測的跨層錯誤/重工，單靠穩定的 OpenAPI/JSON Schema 契約或產生 TypeScript client 型別無法解決。
3. 有具體遷移計畫涵蓋既有 Python 套件責任、wallet 簽章驗證、安全狀態、root privilege boundary、systemd 行為和 rollback。

前端採用 TypeScript 已先於這次 backend framework 選擇完成。Fastify 官方提供 typed route、JSON Schema validation/serialization 及 plugin encapsulation；NestJS 提供應用模組與 dependency injection。兩者支援不同的架構偏好，選擇 NestJS 主要為管理功能成長後的一致組織方式，不代表重寫 Python CLI/runtime 的維護成本已下降。[Fastify v5 TypeScript](https://fastify.dev/docs/v5.12.x/Reference/TypeScript/)、[Fastify v5 validation/serialization](https://fastify.dev/docs/v5.12.x/Reference/Validation-and-Serialization/)、[Fastify v5 plugins](https://fastify.dev/docs/v5.12.x/Reference/Plugins/)。

### 前端何時值得拆出來

選型前的 dashboard 已符合拆分門檻：多個獨立面板共享登入/角色/session 狀態，頁面同時處理多種異步操作和文件上傳，並由單一 Python 字串承載 markup、CSS 與互動程式。React/Vite 前端拆分已完成；這項決策本身不要求重寫 Python CLI 或 ADE runtime。

以下任一項也可作為後續採用其他層的門檻：新增多個真正的 URL route、UI 出現複雜的共享非同步 server state、公開內容開始需要搜尋索引、或量測證明首屏 client-render 造成可觀察的載入問題。只有公開 SEO 或 server-side request data 是產品需求後，再評估 Next.js/SSR。

## 原始分階段遷移方案（採用前）

下列步驟供決策歷程參考。實際實作依上方採用範圍進行。

1. **鎖定行為契約。** 記錄既有 API path、request/response、wallet challenge、Bearer session、角色限制、session 過期和 logout 清除資料規則。管理 origin 與 artifact publisher origin 保持分離；上傳 HTML 不得由管理頁 origin render。
2. **審查 Python HTTP layer。** Python 官方不建議將 `http.server` 用於 production。核對 Nginx proxy、request limits 和目前 handler；若要替換，先用 FastAPI 或 Starlette + Uvicorn 承接原 API，保留 Python domain logic、session 行為、origin 與 root helper 邊界。
3. **加入獨立前端建置。** 建立 `frontend/`，採用 React、Vite 與 TypeScript；Vite dev server proxy 至 Python API。CI/build 工作站安裝 Node LTS 並執行 build 與 type-check，VM 只安裝 Python package 和生成的靜態 assets。
4. **接入 ADES 發布流程。** 決定 build output 放入 `ade` package data 還是部署 source artifact。現有 [`pyproject.toml`](../../pyproject.toml#L20) 只收 `ade/*.json`，而部署 wrapper 從 checkout 以 `uv` 啟動服務，因此要明確加入靜態 assets 的建置、打包和 upgrade/rollback 步驟。Python `/` 回傳 app shell，其餘 assets 使用版本化檔名和合適 cache header。現有 CSP 在 [`environment.py`](https://github.com/djpken/agent-dev-environment/blob/eef08709c3741f700d352ae6e1b9e5640283697a/ade/environment.py#L1882) 將 `script-src` 和 `style-src` 限為 inline；啟用外部 Vite bundle 前須調整成允許同 origin 的 JS/CSS，並確認不再依賴 inline script/style。
5. **逐面板搬移。** 先搬不需授權的 trending，再搬健康狀態與 runs/schedule，最後搬 wallet registration/login、角色操作與 artifact upload。每階段保留舊 API 和同 origin，再核對原有瀏覽器流程與 API 行為。
6. **另案評估全 TypeScript。** 只有 Python runtime 成為明確維護負擔、且共享型別效益無法由 API schema/client generation 取得時，才用 NestJS on Express 做 POC；不要和前端拆分或 HTTP adapter 更換合併成一次重寫。

## SSR 與 Next.js 判斷

Next.js 能以 Node.js server 支援完整功能；static export 可由任意靜態 web server 提供，但 Node server features 不適用。Next.js 官方建議自架時在 Node server 前放 reverse proxy。ADES 已經有 Nginx，可重用這層，但 SSR 仍會多一個需更新、啟停及監控的 VM service。[Next.js deployment](https://nextjs.org/docs/app/getting-started/deploying)、[Next.js self-hosting](https://nextjs.org/docs/app/guides/self-hosting)、[Next.js static exports](https://nextjs.org/docs/app/guides/static-exports)。

現有登入 token 放在 `sessionStorage`，server render 時拿不到該 token。若要 SSR 私有 ADES 資料，需另設可由 server 讀取的登入 cookie/session 流程，並重新設計 CSRF、session 清除、proxy trust 與 server-side authorization。現在只要以 Vite SPA 提供 sign-in shell，登入成功後依原 API 讀取資料即可。Next.js 若採 static export，實質上只增加一層 SPA framework，沒有使用 SSR 的理由。

## 版本與支援狀態快照

以下資料截至 2026-09-23，供初始 POC 對齊，不代表版本鎖定承諾。

- React `19.3.0` 於 2026-09-09 發布；Vite `8.3.0` 於 2026-09-10 發布。[React releases](https://github.com/facebook/react/releases/tag/v19.3.0)、[Vite releases](https://github.com/vitejs/vite/releases/tag/v8.3.0)。
- Vue `3.5.43` 於 2026-09-17 發布；`3.6.0-rc.9` 是預覽版。POC 應鎖定當時穩定版，避免採用 release candidate。[Vue 3.5.43 release](https://github.com/vuejs/core/releases/tag/v3.5.43)、[Vue 3.6.0-rc.9](https://github.com/vuejs/core/releases/tag/v3.6.0-rc.9)。
- Node.js `24` 是 Active LTS；`26` 仍是 Current，官方時程預定 2026-10-28 轉入 LTS。若使用全 TS backend 或建置工具，runtime/build image 選當時受支援的 LTS line，勿把 Current 當 production 預設。[Node.js release status](https://nodejs.org/en/about/previous-releases)、[Node.js official schedule](https://github.com/nodejs/Release/blob/main/schedule.json)。
- Fastify `5.12.5` 於 2026-09-16 發布。Fastify v5 要求 Node.js 20 以上；官方 v5 LTS 表列的測試版本為 Node 20 與 22，因此若以 Node 24 作為 runtime，POC 應先確認選定 patch line 的相容性和支援承諾。[Fastify release](https://github.com/fastify/fastify/releases/tag/v5.12.5)、[Fastify v5 migration guide](https://fastify.dev/docs/v5.12.x/Guides/Migration-Guide-V5/)、[Fastify v5 LTS policy](https://fastify.dev/docs/v5.12.x/Reference/LTS/)。
- Next.js `16.3.6` 於 2026-09-22 發布。版本數字更新很快，只有確定採用 SSR 後才需要鎖定版本與 Node 相容範圍。[Next.js releases](https://github.com/vercel/next.js/releases/tag/v16.3.6)。

## 尚未確認的假設

- 團隊目前對 React、Vue、TypeScript 與 npm build pipeline 的實際熟悉程度未知。維護者熟悉度仍會影響後續模組設計和版本維護。
- ADES 前端仍是一個同 origin 管理頁面，不需要多租戶、服務端渲染或公開 SEO route；若產品方向改變，需重新評估 Next.js。
- installer 目前會在 VM 使用 `npm ci` 安裝鎖定依賴並建置 bundle，VM 也執行 Node service。後續若要改成 CI build artifact，需保留產物可重現性與安裝版本對應。
- 本備忘錄不替目前 Python `ThreadingHTTPServer` 的 production suitability 背書。部署文件顯示 Nginx 作為公網入口，仍需另行檢查 proxy 與應用 handler 的完整威脅模型。
- 尚未量測前端 bundle size、dashboard first contentful render 或目前功能開發/測試耗時，因此選型根據維護邊界和部署模型，不宣稱效能提升。
