# Review workflow context

這個 repo 收納可獨立安裝的 skills 與 prompt。Review workflow 以 `/base` 固定工作起點，再由不同 review variant 檢查變更。

## Prompts

**本地改編 prompt**：
Workflow Pack 中參考外部來源、翻譯並依本地偏好調整的 prompt；其內容由本地維護者決定。
_Avoid_: 上游同步副本、逐字翻譯版

## Baseline

**Base manifest**：由 `/base` 建立的固定 review 起點與 task source。
_Avoid_: moving ref、臨時 baseline

**Review range**：從固定 review 起點到目前 revision 的變更範圍，並排除 review 自己產生的狀態。
_Avoid_: workspace review、single-commit review

## Review variants

**Standards/Spec review**：同時檢查程式碼是否符合 repo standards，以及是否符合 Base manifest 提供的 task source。
_Avoid_: 單一總分 review

**OCR review**：依賴 OCR MCP 的 review variant，保留自己的 findings 與 counter 狀態。
_Avoid_: 單軸 review、Standards/Spec review

**Review variant**：共享 Base manifest、review range 與 task source contract 的獨立 review 入口。
_Avoid_: review mode、alias
