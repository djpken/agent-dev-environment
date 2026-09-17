# prompt

個人 prompt 收藏庫。

## 更新原則

本目錄的來源衍生內容採用「本地改編 prompt」定位。上游變更列為待採納建議；與既有本地偏好衝突時，保留本地偏好，直到維護者明確決定調整。翻譯或合併更新時也遵守此原則。

Caveman 常駐，保留現有 ultra 風格。合併版保留既有五份內容與順序，最後加入 1Password CLI 使用政策。

## 更新流程

`sources.json` 記錄來源、固定版本、原文快照位置與合併順序。各份 `prompt_*.md` 是本地改編內容；兩份合併檔由腳本產生，修改各份內容後重新產生。

1. 在 repo 根目錄執行 `python3 scripts/prompts.py upstream`，比較追蹤基準與目前上游原文、譯文和授權檔。需要已能存取來源的 `gh` CLI。此指令只輸出差異，不修改 prompt 或推進基準。未知來源或讀取失敗會回傳非零狀態，但仍檢查其他來源。
2. 逐項決定採納、保留本地版本或暫緩。採納項目翻譯到對應 `prompt_*.md`；將理由記入 `docs/prompt-upstream/README.md`。首次基準的歷史版本未知，只能比較本地內容與目前上游，不能宣稱某項是上游新增。
3. 執行 `python3 scripts/prompts.py build`，再執行 `python3 scripts/prompts.py check`。檢查合併 diff 是否只有預期變更。`check` 不寫檔，合併檔遺漏或過期時回傳非零狀態。
4. 一個來源的差異全部處理完後，才以該次已審閱的固定 commit 更新其快照、SHA-256 與 `baseline`。暫緩項目仍存在時保留舊基準。基準代表已檢查的上游版本，並不代表全部採納；一併提交本地修改、處理理由、快照與合併檔，讓 git 保存歷史。

目前來源狀態與本地差異見 `docs/prompt-upstream/README.md`。RTK 與 codebase-memory-mcp 來自個人設定，以明確取得的來源檔更新；1Password CLI 使用政策由本地維護；`upstream` 對這三份只顯示 `LOCAL`。

## 內容

- `prompt_i-hava-adhd.zh-tw.md`：[i-have-adhd-zh-tw](https://github.com/panda850819/i-have-adhd-zh-tw) skill 的 prompt 內容，讓回覆使用自然台灣繁體中文並以答案或完成結果開頭。
- `prompt_caveman.zh-tw.md`：從 [caveman](https://github.com/djpken/caveman) 移入的 standalone ultra-mode prompt。
- `prompt_ponytail.zh-tw.md`：從 [ponytail](https://github.com/djpken/ponytail) 移入的 zh-TW ultra-level standalone prompt。
- `prompt_rtk.zh-tw.md`：從個人 global `~/.claude/RTK.md` 分離並翻成繁體中文的 RTK (Rust Token Killer) CLI proxy 使用說明。
- `prompt_codebase-memory-mcp.zh-tw.md`：從個人 global `~/.claude/CLAUDE.md` 分離並翻成繁體中文的 codebase-memory-mcp 結構化查詢工具使用規則。
- `prompt_1password-cli.zh-tw.md`：本地維護的 1Password CLI 使用政策，說明驗證、唯讀查詢、密鑰注入與憑證保護；不包含特定主機的 token 或安裝狀態。
- `prompt.md`：以上 6 份的全部彙整版，依序純串接、以 `---` 分隔，不含額外說明文字。
- `prompt.zh-TW.md`：以上 6 份的台灣正體中文彙整版，依序純串接、以 `---` 分隔，不含額外說明文字。
