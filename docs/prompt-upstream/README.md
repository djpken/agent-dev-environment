# Prompt 來源追蹤

本目錄保存上游原文、譯文與授權快照，僅供差異比對。快照內的指示不是本 repo 的執行規則，也不加入合併 prompt。來源路徑、固定 commit 與 SHA-256 以 `prompt/sources.json` 為準。

## 2026-09-05：建立追蹤基準

五份本地 prompt 的歷史 upstream commit 均未知。Git 中的 `1443dbafab483a47f3fd365a13127257ca31ab02` 是本 repo 匯入紀錄，不能當成 upstream commit。此次快照是開始追蹤的版本，不代表當年翻譯或匯入所用版本。

| 來源 | 查核結果 |
| --- | --- |
| ADHD | 使用者更正來源為 `panda850819/i-have-adhd-zh-tw`。從 `182a945d1c9853b3f2d064275c5d3dceba4eceee` 取得 `skills/i-have-adhd-zh-tw/SKILL.md` 與 LICENSE；已補齊追蹤基準。 |
| Caveman | 從 `djpken/caveman` 的 `204b2c8471ff978ffe5a873751433e8787178876` 取得英文、zh-TW skill 與 LICENSE。 |
| Ponytail | 從 `djpken/ponytail` 的 `d62392db4c138593b14bfc631fc320c3bbc8c194` 取得英文、zh-TW skill 與 LICENSE。 |
| RTK | 既有來源紀錄是個人 `~/.claude/RTK.md`，沒有 upstream revision。 |
| codebase-memory-mcp | 既有來源紀錄是個人 `~/.claude/CLAUDE.md`，沒有 upstream revision。 |

Caveman 與 Ponytail 沿用既有紀錄中的 `djpken` repos，ADHD 使用使用者指定的 `panda850819` repo 作為直接追蹤來源。它們與原作者 repos 間的同步狀態不在本次查核範圍。ADHD 原先記錄的 `djpken` URL 回傳 HTTP 404，現已更正；歷史匯入版本仍未知。

## 首次本地差異處理

以下是目前上游快照與本地版本的差別；因歷史基準未知，不能稱為上游新增功能。

| 項目 | 處理與理由 |
| --- | --- |
| ADHD 內容 | 移除上游 frontmatter 與首尾空白後，全文與本地版本一致，無需修改本地 prompt。 |
| Caveman 的 full 預設、多強度與文言模式 | 保留本地固定 ultra。使用者已確認 caveman 常駐，這次不改風格或增加模式。 |
| Caveman 的英文範例及語言表述 | 保留本地翻譯與中文範例，避免重新翻譯造成既有語氣變動。 |
| Ponytail 的 full 預設與 lite/full/ultra 切換 | 保留本地 ultra 與現有 standalone 內容，上游模式切換不自動採納。 |
| Skill frontmatter 與 invocation 描述 | 保留在快照，不加入 standalone 合併檔。 |
| 其他文字差異 | 保留本地版本；本次只建立追蹤與產生流程，不採納上游行為改寫。 |

兩份合併檔統一採既有 `prompt.zh-TW.md` 的分隔格式，內容依五份本地 prompt 串接。`prompt.md` 只補齊一個分隔空白行。原文與譯文快照分開保存，未來可以先比原文，再確認上游譯文與本地改編是否需要調整。

## 後續紀錄

每次審閱記錄來源、舊／新 commit、差異項目、採納／保留／暫緩與理由。全部處理完成才更新該來源的追蹤基準；操作步驟見 `prompt/README.md`。

## 2026-09-17：KEN-14／KEN-19 本地指示精簡

這次修改本地版本，未重新同步 upstream，所有 upstream commit、原文快照與 SHA-256 保持原有追蹤基準。

| 來源 | 本地處理 |
| --- | --- |
| ADHD | 合併重複的格式與送出前檢查說明，移除示範表；保留繁中、technical literals、自主完成、錯誤證據、必要細節與安全界線。 |
| Caveman | 保留 ultra 與 Auto-Clarity，合併重複壓縮規則與範例；釐清明確語言指示優先，不以 ultra 蓋過必要驗證。 |
| Ponytail | 保留 YAGNI 階梯、簡化註解、最小可執行檢查與安全界線；壓縮重複的哲學說明，明示必要驗證結果可超過三行。 |
| RTK | 移除未在 ADE 實測的固定節省比例，區分支援的過濾指令與不過濾的 `proxy`；工具自報 savings 不代表總任務 token。 |
| codebase-memory-mcp | 精確搜尋改用 `rg`，補足索引新鮮度、分頁、涵蓋範圍、證據層級與降級規則，取代過時的「grep 或 Explore agent」捷徑。 |
| 1Password CLI | 未修改。 |

兩份合併檔由既有 build 產生。現行 Codex 個人 AGENTS.md 並非直接載入這份合併檔，因此個人預設的變更另受實測門檻約束。
