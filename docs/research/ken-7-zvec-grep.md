# KEN-7：zvec-grep provider 研究紀錄

- **狀態：** integration-only candidate，暫不納入 ADE curated/default providers
- **資料查閱日：** 2026-09-24
- **查核版本：** npm `@zvec/zvec-grep@0.2.2`；Git tag `v0.2.2`，commit `b1a9148e26a7bc9bd4a52229ffb7532d3063793d`
- **範圍：** zvec-grep 的 workspace retrieval/indexing 能力、CLI/MCP 介面、執行和安裝方式、依賴、授權、維護風險，以及它和 ADE Capability provider 邊界的適配性。

## 判斷

zvec-grep 提供獨立的 workspace retrieval/indexing 能力，並持有自己的索引、Embedding 設定與服務狀態，符合 ADE 的 Capability provider 定義。ADE 若開始整合，建議先以使用者明確選用的 integration-only candidate 試接，讓 zvec-grep 擁有索引及檢索業務狀態，ADE 管理安裝版本、啟停、連線和健康檢查。這符合 ADE 對 provider 與 adapter 的責任分界，也不把搜尋能力塞入 Workflow Pack。

目前不納入 curated/default provider 清單。`0.2.2` 仍處於快速變動的 `0.2` 發布線；GitHub Releases 頁最新正式 release 仍是 `v0.2.0`，npm `latest` 已到 `0.2.2`，GitHub `main` 的 `package.json` 卻是 `0.2.1`。`main` 文件的 CLI 介面也已與 npm `0.2.2` 不同。索引升版與重建另有尚未解決的使用者回報。這些因素使穩定介面、升級相容性和安裝重現性尚不足以支撐預設散布。[ADE 職責劃分](../ade-responsibilities.md#ownership)、[GitHub Releases](https://github.com/zvec-ai/zvec-grep/releases)、[npm 套件中繼資料](https://registry.npmjs.org/@zvec/zvec-grep)、[v0.2.2 package.json](https://github.com/zvec-ai/zvec-grep/blob/v0.2.2/package.json)、[CLI 介面差異 issue #147](https://github.com/zvec-ai/zvec-grep/issues/147)

## 能力與資料範圍

`zg` 將 managed ripgrep、BM25 lexical retrieval 和 vector semantic retrieval 放在同一個 workspace 搜尋入口。索引查詢可做 hybrid、lexical 或 vector 路由；`zg query --rg` 可在不建索引的情況下執行完整的 literal 或 regex 搜尋。結果以檔案路徑、來源位置和受限片段呈現，適合讓 agent 先定位證據，再用精確搜尋跟進。[v0.2.2 README](https://github.com/zvec-ai/zvec-grep/blob/v0.2.2/README.md)、[v0.2.2 retrieval pipeline](https://github.com/zvec-ai/zvec-grep/blob/v0.2.2/docs/04-pipeline.md)、[CLI command source](https://github.com/zvec-ai/zvec-grep/blob/v0.2.2/src/cli/commands.ts)

索引器對主要程式語言保留 symbol、signature、breadcrumbs 等結構；Markdown 依標題切段；HTML、純文字、JSON、JSONC、TOML、YAML、CSV 及其他可讀文字可走文字抽取。支援格式仍有明確邊界：PDF、Office 文件、封存檔、影音及資料庫等目前會略過；圖片須明確納入，而且所選 Embedding 模型需支援圖片。索引位於 workspace 根目錄的 `.zvec-grep/`，預設套用 repository ignore 規則並排除 `.git` 和索引本身。[v0.2.2 retrieval pipeline](https://github.com/zvec-ai/zvec-grep/blob/v0.2.2/docs/04-pipeline.md)

## 介面與執行方式

npm `0.2.2` 使用 subcommand CLI：`zg index`、`zg query`、`zg status`、`zg install`、`zg server on|off|status`。索引查詢支援 `auto`、`server`、`direct` 模式。`auto` 使用已就緒的 server，否則在目前程序直接執行；`server` 要求 daemon 可用；`direct` 不啟動 daemon。長時間 agent 使用可由本機 zvec-grep server 管理索引更新、重用模型並提供 MCP endpoint。[v0.2.2 README](https://github.com/zvec-ai/zvec-grep/blob/v0.2.2/README.md)、[v0.2.2 CLI guide](https://github.com/zvec-ai/zvec-grep/blob/v0.2.2/docs/02-cli.md)、[v0.2.2 server guide](https://github.com/zvec-ai/zvec-grep/blob/v0.2.2/docs/06-server.md)

MCP 使用 Streamable HTTP，預設 endpoint 為 `http://127.0.0.1:7999/mcp`。預設 `agent` toolset 只註冊 `zvec_grep_search`；`full` toolset 才加入 managed ripgrep、index、index drop、index status 和 server status。MCP 文件要求 agent 不得自行建立、重建或刪除持久索引。Server 可設定 loopback Bearer token；這項認證與 Embedding provider credential 分開管理。[v0.2.2 MCP guide](https://github.com/zvec-ai/zvec-grep/blob/v0.2.2/docs/03-mcp.md)、[HTTP server source](https://github.com/zvec-ai/zvec-grep/blob/v0.2.2/src/daemon/http-server.ts)

**ADE 接入形狀建議：** 若 POC 使用 agent MCP，先只開預設 search toolset。依 ADE schema 可建模為 `shared-local` lifecycle、`http` transport 和 loopback URL；使用者選擇啟用後再由 ADE 管理 server lifecycle 和 health check。`zg` 索引、Embedding 模型、工作區清單及其狀態都由 provider 擁有；adapter 只保存連線和整合狀態，不另建索引狀態資料庫。[ADE provider schema](../../ade/provider.schema.json)、[ADE 職責劃分](../ade-responsibilities.md#ownership)、[MCP guide](https://github.com/zvec-ai/zvec-grep/blob/v0.2.2/docs/03-mcp.md)

## 安裝、依賴與重現性

`@zvec/zvec-grep@0.2.2` 是查核日 npm `latest`，套件宣告 Node.js `>=22`，README 列出 macOS、Linux 和 Windows 支援。執行入口為 npm binary `zg`。套件直接依賴 `@zvec/zvec`、`@vscode/ripgrep`、Hugging Face tokenizers/transformers、MCP SDK、Tree-sitter WASM/runtime、`jsonc-parser` 和 `zod`；`node-llama-cpp@3.18.1` 是 optional dependency。zvec 和 llama.cpp 相關元件包含平台相依的 native/runtime 支援，ADE 需要依實際作業系統與 CPU/GPU 組合驗證。[v0.2.2 README](https://github.com/zvec-ai/zvec-grep/blob/v0.2.2/README.md)、[v0.2.2 package.json](https://github.com/zvec-ai/zvec-grep/blob/v0.2.2/package.json)、[npm `0.2.2` metadata](https://registry.npmjs.org/@zvec/zvec-grep/0.2.2)

查核日 npm metadata 將 `latest` 指向 `0.2.2`，發布時間為 2026-09-07 07:53 UTC，tarball 為 `zvec-grep-0.2.2.tgz`，SRI 為 `sha512-6xsF21zgUh98W3BmH7+Nz/Esdx4Ps3ibs+DHluh/uo2O45xCMeM+pkmdRlcagkONHRO2pM2KiGgKHVmRGKxBtA==`。Git tag `v0.2.2` 指向 `b1a9148e26a7bc9bd4a52229ffb7532d3063793d`。這提供固定套件版本、固定 tarball 雜湊與固定原始碼 tag 的依據；npm metadata 未提供 `gitHead`，因此目前無法只靠 registry metadata 證明 tarball 和該 tag 完全對應。[npm `0.2.2` metadata](https://registry.npmjs.org/@zvec/zvec-grep/0.2.2)、[Git tag v0.2.2](https://github.com/zvec-ai/zvec-grep/tree/v0.2.2)、[GitHub Releases](https://github.com/zvec-ai/zvec-grep/releases)

repo 的 `v0.2.2` 有 lockfile v3，可固定 repo 開發環境的解析樹；package manifest 的發佈檔案清單不含 lockfile，全球安裝命令本身只固定 zvec-grep 根套件版本。依賴宣告包含 semver range，例如 `@zvec/zvec: ^0.7.1` 和 Transformers.js。若 ADE 要可重現安裝，應由 ADE 自己固定 Node.js patch line、npm package version、完整 dependency lock/integrity 與安裝來源，不以 upstream `package-lock.json` 取代 ADE 的鎖定責任。[v0.2.2 package.json](https://github.com/zvec-ai/zvec-grep/blob/v0.2.2/package.json)、[v0.2.2 package-lock.json](https://github.com/zvec-ai/zvec-grep/blob/v0.2.2/package-lock.json)、[npm `0.2.2` metadata](https://registry.npmjs.org/@zvec/zvec-grep/0.2.2)

第一次建立 local embedding index 會從 Hugging Face 下載所選模型，預設快取在 `~/.zvec-grep/models`；下載失敗時，文件記載會改用固定且有 integrity check 的 ModelScope 副本。Remote Embedding 會將授權範圍內的 workspace/query 內容送至設定的服務商；credential 本身不等於資料傳送授權，必須另外授權。ADE 安裝時要能取得 npm 套件，首次本機模型使用也需要下載網路；正式設定應預設 local embedding，remote provider 維持使用者明確開啟。[v0.2.2 Embedding guide](https://github.com/zvec-ai/zvec-grep/blob/v0.2.2/docs/07-embedding.md)

## 授權與維護風險

zvec-grep 自身宣告 Apache-2.0，repo 含對應 LICENSE。ADE 若散布 npm 套件及其依賴樹，仍需另外盤點第三方 dependency licenses、native artifacts 和各平台發佈內容。[v0.2.2 package.json](https://github.com/zvec-ai/zvec-grep/blob/v0.2.2/package.json)、[v0.2.2 LICENSE](https://github.com/zvec-ai/zvec-grep/blob/v0.2.2/LICENSE)

目前發佈面有可觀察的版本介面落差：GitHub Releases 頁最新 release 是 2026-08-27 的 `v0.2.0`；npm `latest` 是 2026-09-07 發布的 `0.2.2`；查核日 GitHub `main` 的 package manifest 是 `0.2.1`。GitHub issue #147 和仍開啟的 PR #148 記錄 `main` 的 `docs/` 已改為 `zg --index` flag-first CLI，但 npm `0.2.2` 還使用 `zg index`、`zg query` subcommands。不能拿 `main` 文件直接設定 npm `0.2.2` 的 provider command；POC 必須固定 tag/package，並以已安裝版本的 `zg help` 作為參數依據。[GitHub Releases](https://github.com/zvec-ai/zvec-grep/releases)、[main package.json](https://github.com/zvec-ai/zvec-grep/blob/main/package.json)、[issue #147](https://github.com/zvec-ai/zvec-grep/issues/147)、[PR #148](https://github.com/zvec-ai/zvec-grep/pull/148)

Roadmap 將專案標為持續開發中的 public preview，並把跨平台安裝、incremental indexing、server recovery、freshness、CLI/MCP contracts 和 index compatibility 列為尚待加固或穩定的工作。查核日仍開啟的使用者 issue #139 回報 `0.2.1` 建立的索引在 `0.2.2` 查詢失敗，但 status 仍回報 ready；issue #140 回報 `0.2.2` 的 rebuild 刪掉舊索引卻沒有建立新索引。修正 PR #143 仍開啟，review comment 又指出 manifest commit 失敗仍可能留下新舊索引不一致。這些是 issue/PR 作者的回報和測試，不是本次獨立重現；它們足以要求 ADE 對索引做備份、升版和回復測試，不能宣稱缺陷已修好。[v0.2.2 roadmap](https://github.com/zvec-ai/zvec-grep/blob/v0.2.2/docs/08-roadmap.md)、[issue #139](https://github.com/zvec-ai/zvec-grep/issues/139)、[issue #140](https://github.com/zvec-ai/zvec-grep/issues/140)、[PR #143](https://github.com/zvec-ai/zvec-grep/pull/143)

Remote Embedding 的 watcher 另有使用者 issue #137 回報持續重複送出 embedding request，造成高額 API 用量。這是單一環境的回報，未在本次重跑；試用前應先用 local model，若測 remote model，需設成本與流量上限並監看實際請求。[issue #137](https://github.com/zvec-ai/zvec-grep/issues/137)

## ADE provider 對照

ADE 將專用能力與其業務狀態交給 Capability provider；Composition repository 管理選型、版本、installer、設定、授權、更新和 rollback；adapter 保存連線與 session 等整合狀態。provider manifest 要明確宣告 capability、lifecycle、transport、command、health check 和 permissions。zvec-grep 適合以上述 provider 身分整合，由 Workflow Pack 宣告何時使用搜尋能力，讓 Runtime 管理 provider lifecycle。[ADE 職責劃分](../ade-responsibilities.md#ownership)、[provider schema](../../ade/provider.schema.json)、[domain glossary](../../CONTEXT.md#environment)

目前 ADE 的 user config `extensions` 已接受符合 provider schema 的第三方 manifest，並支援 `host-spawned`、`shared-local` lifecycle，以及 `cli`、`stdio`、loopback `http` transport。這是明確設定後的 provider extension 接入點，不是 zvec-grep 的套件安裝器、curated registry 或預設安裝項目。可重現接入仍需固定 Node.js、npm 套件版本與完整依賴 integrity，並驗證 `zg` 的啟動方式能符合 ADE lifecycle；現有 schema 本身不會安裝 npm package。[ADE 擴充與 lifecycle](../ade-runtime.md#擴充與-lifecycle)、[provider schema](../../ade/provider.schema.json)、[extension 設定驗證](../../ade/core.py)

依其本機讀取 workspace、在 workspace 根目錄寫入 `.zvec-grep`、使用者快取及 loopback server 行為，POC manifest 預期需要 `workspace-read`、`workspace-write`、`local-state`、`listen-loopback`；首次下載 local model 需 `external-network`。這是依 ADE schema 權限名稱做的整合推論，不代表 zvec-grep 本身具有 ADE manifest 或 sandbox 保證。ADE 文件也明示目前 provider permissions 是設定宣告，不會自動構成作業系統隔離。[provider schema](../../ade/provider.schema.json)、[ADE provider 權限與邊界](../ade-responsibilities.md#設定與-lifecycle)

## 與既有工具的邊界

| 工具 | 主要責任 | 和 zvec-grep 的切分 |
| --- | --- | --- |
| `codebase-memory-mcp` | ADE 的程式碼知識圖譜，提供 symbol、caller/callee、dependency 與 coverage evidence 等結構查詢。 | 結構與影響分析仍走 graph；zvec-grep 提供 workspace 內 hybrid、semantic、lexical retrieval。精確文字和檔案路徑仍用 `rg`。檢索排序不能取代呼叫鏈或 coverage evidence。 |
| Headroom | 選用的 context/token 處理能力，目前預設關閉。 | 不負責程式碼索引或檢索。需要時才與搜尋能力組合，分開驗收查找效果與 context/token 效果。 |

因此 workflow 可先以 `rg` 處理已知字串、以 graph 處理結構與呼叫關係；需要自然語言或 hybrid workspace retrieval 時才查 zvec-grep。`agent` MCP toolset 預設只暴露搜尋；索引建立、刪除及重建不應交給 agent 自行操作。Headroom 可獨立選用，避免搜尋工具及 context 處理同時變動而無法判斷結果來源。[ADE code search policy](../../AGENTS.md#程式碼搜尋與導覽)、[Headroom 職責](../ade-responsibilities.md#ownership)

這次只完成評估與 integration boundary，尚未在 ADE 安裝、啟動或測試 zvec-grep，也沒有建立可重現的 package installer/configuration；故先保留 integration-only candidate，不宣稱已完成 ADE 接入。

## 證據層級與未驗證限制

- 上游 claim 以官方 README、`v0.2.2` Git tag source/docs、GitHub Releases、GitHub issues/PR 與 npm registry metadata 為一手證據。查核日以固定 tag `v0.2.2` 和 npm 版本 `0.2.2` 為準，並另記 `main`/release/package 不同步。
- ADE 結構查詢為 Tier 2 Verify，專案 `agent-dev-environment` 已在本次工作階段重建索引，generation `2026-09-24T14:01:46Z`，index status 為 indexed，3222 nodes、8179 edges，沒有 skipped 或 parse-partial 檔案。針對 `ade/` 搜尋 provider、manifest、lifecycle、health、argv、install、rollback 等符號，並查閱 `ade.core.compose`、`ade.core.authorize`、`ade.core.health`、`ade.core.clean_env`、`ade.core.install`、`ade.core.rollback` 與 `ade.cli.supervise` 的圖譜路徑和原始碼片段。對 `ade/cli.py`、`ade/core.py`、`ade/provider.schema.json`、`ade/environment.py`、`ade/scheduler.py`、`ade/sync.py` 呼叫 `check_index_coverage`，結果為 `no_recorded_issue`、`metadata_match`、generation match，仍屬 best-effort，不能證明內容絕對完整。部分 `install`/`plan` 關聯結果含不相干的 `TrendingService` 候選，因此整合結論以原始碼片段為準。索引將 `docs/`、`deploy/` 標為 deliberately not indexed；文件與 schema 另直接閱讀。
- 本次沒有安裝或執行 zvec-grep，沒有檢查實際 ADE provider lifecycle、不同作業系統 native dependency 相容性、效能、資源需求或模型下載。GitHub issue/PR 的使用者回報沒有獨立重現；依賴樹的授權盤點也未執行。這些項目需在後續 POC 補查。

## Upstream recheck: lifecycle and index safety

- **查閱日：** 2026-09-24
- **範圍：** npm `latest`、Git tags/releases、npm 與 GitHub README/CLI 文件、`v0.2.2` server lifecycle 原始碼，以及 issues #139/#140 和 PR #143 的目前狀態。

### 發布與 CLI 介面

截至查閱日，發布面有以下差異：

| 上游來源 | 查到的狀態 | 對接影響 |
| --- | --- | --- |
| [npm registry](https://registry.npmjs.org/@zvec/zvec-grep) | `dist-tags.latest` 是 `0.2.2`，發布時間為 2026-09-07。registry README 使用 `zg index`、`zg query` 等 subcommand。 | npm 最新版的文件介面仍是 command-first。 |
| [Git tag v0.2.2](https://github.com/zvec-ai/zvec-grep/tree/v0.2.2) | tag 指向 `b1a9148e26a7bc9bd4a52229ffb7532d3063793d`，package version 為 `0.2.2`。該 tag 的 [CLI guide](https://github.com/zvec-ai/zvec-grep/blob/v0.2.2/docs/02-cli.md) 同樣使用 `zg index`、`zg query`。 | npm 最新版和 `v0.2.2` tag 的 CLI 慣例一致。 |
| [GitHub Releases](https://github.com/zvec-ai/zvec-grep/releases) | Releases 頁面的 Latest 仍是 2026-08-27 發布的 `v0.2.0`；`v0.2.2` 有 Git tag，但沒有對應的 GitHub Release。 | GitHub Release 頁落後 npm 和 tag。 |
| GitHub `main` | [`package.json`](https://github.com/zvec-ai/zvec-grep/blob/main/package.json) 仍標 `0.2.1`；[`docs/02-cli.md`](https://github.com/zvec-ai/zvec-grep/blob/main/docs/02-cli.md) 改成 `zg --index`、`zg --status` 等 flag-first 介面。[`README.md`](https://github.com/zvec-ai/zvec-grep/blob/main/README.md) 同時保留 `zg index` 範例與 `zg --index` 用法。 | `main` 的 package version、README 和 CLI guide 彼此也未完全同步。不要把 `main` 文件命令直接套用到 npm `0.2.2`。 |

### Server lifecycle 與 ADE 對照

`v0.2.2` 的 [server guide](https://github.com/zvec-ai/zvec-grep/blob/v0.2.2/docs/06-server.md) 和 [CLI guide](https://github.com/zvec-ai/zvec-grep/blob/v0.2.2/docs/02-cli.md) 明確區分：`zg server on` 啟動背景 daemon，`zg server run` 留在前景，`zg server off` 停止 daemon；`zg server status --check-ready` 可用於 readiness check。HTTP server 只接受 loopback listen address，每個 zvec-grep home 只允許一個 server。文件也提供 `ZVEC_GREP_INSTALL_SKIP_SERVER=1`，供其他 process manager 負責啟動 server。

`v0.2.2` 的 [`server-controller.ts`](https://github.com/zvec-ai/zvec-grep/blob/v0.2.2/src/daemon/server-controller.ts) 顯示 `server on` 以 detached child process 啟動並呼叫 `unref()`。`server off` 先送 shutdown request，再嘗試送出 `SIGTERM`，逾時後才送 `SIGKILL`。[`runtime.ts`](https://github.com/zvec-ai/zvec-grep/blob/v0.2.2/src/daemon/runtime.ts) 則在前景 server 收到 `SIGINT` 或 `SIGTERM` 時關閉 backend、HTTP server 並釋放 instance lock。

依 ADE manifest 對 lifecycle 的定義，`zg server run` 加上 `status --check-ready` 在介面上可配合 `shared-local` 的前景 loopback HTTP process 監督；`zg server on` 會脫離啟動它的程序，不適合作為由 ADE 前景 supervisor 直接管理的 child。這是根據兩邊文件與原始碼做的整合判斷，尚未在 ADE 執行驗證。`host-spawned` 的實際交接、停止與 host 重連行為也尚未做 POC，因此不能只憑 CLI 存在就確認相容。

### Index 升版與 rebuild 問題

截至查閱日，[issue #139](https://github.com/zvec-ai/zvec-grep/issues/139) 和 [issue #140](https://github.com/zvec-ai/zvec-grep/issues/140) 都仍為 Open。#139 的作者回報 0.2.1 建立的索引升到 0.2.2 後查詢失敗，但 `zg status` 仍回報 ready；GitHub issue 頁目前沒有列出關聯 PR。#140 的作者回報 `zg index --rebuild` 可能先刪除既有索引，之後因路徑問題快速失敗。這些內容是作者提供的環境和重現步驟，本次沒有自行執行驗證。

[PR #143](https://github.com/zvec-ai/zvec-grep/pull/143) 對應 #140，提出先在暫存位置建立新索引，成功提交 manifest 後才替換 live index。PR 目前仍為 Open，GitHub API 的 `merged` 為 `false`、`merged_at` 為 `null`，且頁面要求至少一個 approving review。PR 在 2026-09-17 的 review 指出 manifest 發布失敗仍可能造成新舊索引不一致，也指出路徑調整可能誤拒合法 POSIX 路徑；後續 commit 標題分別提到保留 backup 至 manifest 發布、修正 drive-like POSIX 路徑判斷，以及拆分跨平台測試。這代表 PR 有後續修訂，不能視為已合併或已發布的修復。npm `latest` 仍是 `0.2.2`，GitHub Releases Latest 仍是 `v0.2.0`，目前沒有從這些來源確認到包含 PR #143 修訂的正式發布版。#139 的舊索引讀取問題也沒有在本次找到已合併或已發布的獨立修復。

### 尚未證實

- #139/#140 都是上游作者的 issue 回報，未在本工作區重現。PR #143 的修訂尚未合併；不能據 PR 標題或作者測試計畫推定 rebuild 安全性已修好。
- 尚未在 ADE 啟動 zvec-grep server 或建立 workspace index。隔離 POC 已安裝 npm package 並產生 lockfile；`zg version` 輸出 `0.2.2`，`@zvec/zvec` prebuilt-binding check 成功。npm `ci` 提示五個相依套件的 install scripts 未執行；Node.js v26.7.0 / npm 11.19.0 的目前安裝狀態和限制記在 2026-09-25 進度段。尚未執行 tests、MCP smoke 或索引升版回復驗證。`shared-local` 仍需在隔離 workspace 驗證啟停、readiness、index 備份與 rollback。Linear 留言更新後，issue 維持 In Progress。

## ADE 安裝與 rollback 對照

ADE `core.install` 只有在 provider 名稱出現在 lock 的 `artifacts` 時，才會依平台選 artifact、下載並驗證 SHA-256。user-config `extensions` 若沒有對應鎖定 artifact，install 只會檢查授權並執行 manifest 的既有 `health` command；不會呼叫 npm、解析 npm dependency tree 或安裝 `@zvec/zvec-grep`。因此目前 extension manifest 只能接入已安裝且可執行的 `zg`，不能單獨提供可重現安裝。[ADE artifact 與 provider install](../../ade/core.py#L197)、[install flow](../../ade/core.py#L276)

ADE `health` 以 manifest argv 執行 health command，最多等 20 秒，要求 exit code 為 0，且輸出必須包含 manifest 宣告的 provider version。執行環境只保留 `PATH`、locale/temp、`SYSTEMROOT`、`HOME` 和 manifest 明列的 `env_vars`。若 provider 用外部 embedding service，credential 必須明列 env var；ADE 不會自動把任意 embedding 設定轉成 provider 環境變數。[health 與環境白名單](../../ade/core.py#L219)

ADE rollback 只讀目前 generation 的 `previous.json`，再原子切回上一個 release generation。它不會回復 generation 外的 npm 全域安裝，也不會快照或還原位於 workspace 或 `HOME` 的 `.zvec-grep` index/model cache。配合上游 #139/#140 的未解回報，POC 應分開固定 provider package、保留 index 備份，並驗證套件升版失敗後能恢復原索引與查詢能力。[generation pointer rollback](../../ade/core.py#L337)、[ADE rollback/data 說明](../ade-runtime.md#擴充與-lifecycle)

可供下一階段驗收的最小 POC 範圍：固定 Node.js 與 `@zvec/zvec-grep@0.2.2` 的來源及完整 dependency integrity；用 ADE 可管理的 artifact/installer 部署，不依賴未鎖版本的全域 npm 狀態；以前景 `zg server run` 接 `shared-local`，health 同時確認 readiness 與版本；在隔離 workspace 驗證首次建索引、查詢、升版、rebuild 失敗、索引備份還原和 ADE generation rollback。這是根據目前 ADE install/lifecycle 契約與上游 issue 推導的驗收建議，尚未執行，也不代表 schema 已具備 npm installer。

## 2026-09-25 POC 配置進度

新增 `integrations/zvec-grep-poc/`，以 `package.json` 精確固定 `@zvec/zvec-grep@0.2.2`，npm lockfile 固定解析後的依賴樹。Node.js `v26.7.0`、npm `11.19.0` 環境執行 `npm ci`，安裝 192 個 packages；npm 提示 `@zvec/zvec`、`node-llama-cpp`、`onnxruntime-node`、`protobufjs`、`sharp` 的 install scripts 未執行。已直接執行 `@zvec/zvec` install check，找到 Linux x64 預編譯 binding 並成功退出。其他 scripts 未執行，local model runtime 能力尚未確認。

`configure.py` 會在忽略的 `.ade/zvec-grep-poc-user.json` 寫入 user extension manifest，command/health 使用本機安裝的絕對執行路徑；manifest 採 `shared-local`、`http`、`http://127.0.0.1:7999/mcp`、前景 `zg server run` 和預設 agent search toolset。Grants 包含 workspace read/write、local state、loopback listen 和下載 local model 所需的 external network。這些 grants 是 ADE 啟動政策，不是 OS sandbox。

已用 `ade plan` 與 `ade apply` 把此設定放進忽略路徑 `.ade/zvec-grep-poc-runtime` 的隔離 ADE root，provider health command 回報版本 `0.2.2`，且沒有 provider grant 阻擋。沒有套用到預設 ADE root，沒有接入 host config，也沒有啟動 server 或建立 index。此 POC 有完整 npm dependency lock，但目前沒有固定 Node.js patch version；ADE 尚未將 npm dependency tree 納入其 lock/artifact installer。
