# P0–P2 交付驗證

日期：2026-09-18。範圍：Linear KEN-22 與 KEN-23 至 KEN-29。所有模型執行均為 fake，沒有發送 reviewer、Judge 或付費 preflight 請求。

## 交付對照

| Issue | 交付與主要驗證 |
| --- | --- |
| KEN-23 | 四階段公開 CLI、兩個 commit 的合成 PR、五項指標、immutable artifacts；`test_two_commits_complete_public_workflow` |
| KEN-24 | 固定官方 converter/scorer/dataset source hashes、AACR-shaped fixture、checksum／重複／無效或反向 range 拒絕；`test_dataset_and_range_validation` |
| KEN-25 | Codex exec 契約 fake、兩個 axis processes、bubblewrap、skill/range/model 證據、usage coverage、fake op reference 邊界；`test_timeout_kills_descendant_and_secret_is_redacted` |
| KEN-26 | commit/workspace snapshots、staged/untracked/mode、必要相依、symlink／憑證／競態防護；`test_workspace_snapshot_survives_source_changes_and_preserves_modes`、`test_snapshot_detects_concurrent_source_edit` |
| KEN-27 | append-only attempts、SIGINT resume、完整 child 回收、成功結果不重跑、失敗 usage 保留；`test_resume_preserves_success_and_failed_attempt_cost`、`test_interrupt_and_resume_keeps_successful_attempt` |
| KEN-28 | 獨立 Judge 重評、官方 scorer 邊界 fixtures、PR cluster bootstrap、共同 round 配對；`test_reevaluation_uses_new_judge_without_new_reviewer_runs`、`test_partial_round_pairing_uses_only_common_measurements` |
| KEN-29 | locked benchmark environment、公開 demo、完整／incomplete／resumed 報告樣本、操作與 pilot 限制文件 |

## 執行的檢查

```bash
uv run --project benchmarks/aacr --frozen mypy --config-file benchmarks/aacr/pyproject.toml
uv run --project benchmarks/aacr --frozen python -m unittest discover -s tests -p test_aacr_bench.py -v
uv run --project benchmarks/aacr --frozen python -m benchmarks.aacr.demo --out /tmp/ade-aacr-demo-delivery
uv run --frozen python -m unittest discover -s tests -v
```

Benchmark 專用 locked 環境的測試、typecheck 與 demo 已執行。後續新增的 interrupt、snapshot race、credential prefix 與 paired-round fixtures 也經個別 CLI 測試及最終 repository suite 驗證。Typecheck 涵蓋 11 個自有 source files；vendor 保持上游原樣，以 lockfile 驗證 bytes。

官方 dataset 的 lock hash 取自固定 commit 的實際 JSON bytes；上游 `.meta.json` 指向 `main` 且 hash 不同，沒有沿用浮動 URL 或該 checksum。

## Standards

獨立 reviewer 發現 GitHub token prefix matcher 漏掉底線形式，以及中斷造成半行 JSON 時會覆寫 interruption 狀態。兩項均先用公開 CLI 測試重現，再修正並複驗關閉。最終 remaining findings：0。

另外將 PR bundle 從全部 refs 縮小到指定 head 可達歷史；原子寫入與 log 遮罩增量也已檢查。

## Spec

獨立 reviewer 發現相同 credential matcher 缺口，以及部分完成時以 PR ID 聚合但未先交集 reviewer/Judge round。已修正兩者；配對報告與 bootstrap 共用共同 measurement 集合，回歸測試通過。最終 remaining findings：0。

Standards：0 未解；Spec：0 未解。圖譜為 Verify 層級，vendor 屬索引排除範圍，以固定版本原始碼與 hash 補查；圖譜的 `asyncio.run` 同名誤連結已以原始碼校正。

## 保留的限制

這份交付驗證的是離線工具與 fake 契約。真實 Codex 載入指定 skill、provider 接受 `gpt-5.6-luna`／`ultra`、完整子代理 usage、真實 1Password wrapper 與模型費率仍屬後續 pilot。所有結果為 smoke-only、report-only；不宣稱真實模型品質提升、不自動 pass，也不修改 production ADE CLI 或全域 host 設定。
