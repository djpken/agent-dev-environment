# AACR workflow 比較工具

這是 KEN-22 至 KEN-29 的 P0–P2 開發工具，以公開 CLI 比較兩版 Standards/Spec workflow 的離線執行產物。執行端固定使用 Codex CLI 契約的 fake executable，Judge 使用官方 deterministic mock。所有報告都標示 `smoke-only`、`report-only`，不代表真實模型品質，也不判定 merge 是否通過。

工具與 `ade` production runtime 分開。需要 Linux、Git、Python 3.11+、`uv` 與可建立 user/PID/network namespace 的 `bubblewrap`。不支援隔離的主機會停止執行。測試不需要模型憑證。

## 教學：從乾淨環境產生報告

在 repository 根目錄執行。`--out` 必須指定不存在、位於來源 worktree 外的資料夾：

```bash
uv sync --project benchmarks/aacr --frozen
uv run --project benchmarks/aacr --frozen python -m benchmarks.aacr.demo --out /tmp/aacr-demo
```

Demo 建立 temporary Git repositories 形狀的合成來源與 AACR raw-data fixture，再依序呼叫公開命令。它會執行：

1. 兩個 commits 的 prepare、run、evaluate、compare。
2. 舊 commit 對未提交工作區快照的比較，保存第一次失敗的 incomplete 報告。
3. 續跑缺失工作並保留前次成本。
4. 更換獨立 Judge 設定重評，保留原 evaluation，不重跑 reviewer。

終端輸出兩種模式的初始與最終 Markdown 報告路徑。同資料夾內的 `report.json` 包含詳細數字與 provenance。公開示範樣本在 [samples/](samples/)。

## 操作：分階段執行

複製 [config.example.json](config.example.json)，填入 workflow repo、版本、dataset 路徑及 checksum。`dataset.repositories` 對應每個選中案例的 `owner/repo` 與已具備 base/head commits 的本機 repo。資料取得及 repository clone 可預先完成；四階段 CLI 不下載資料、不呼叫模型。

```bash
uv run --project benchmarks/aacr --frozen python -m benchmarks.aacr prepare \
  --config /tmp/aacr-config.json --out /tmp/aacr-experiment
uv run --project benchmarks/aacr --frozen python -m benchmarks.aacr run \
  --out /tmp/aacr-experiment
uv run --project benchmarks/aacr --frozen python -m benchmarks.aacr evaluate \
  --out /tmp/aacr-experiment
uv run --project benchmarks/aacr --frozen python -m benchmarks.aacr compare \
  --out /tmp/aacr-experiment --evaluation EVALUATION_ID
```

`evaluate` 回傳新的 `evaluation_id`。每次重評都要將該 ID 傳給 `compare`，兩組不能拼接不同 evaluation。

`run --max-jobs 1` 可在一個 attempt 後停止，用於展示部分完成。再次執行 `run` 就是 resume；成功項目保持不變，失敗項目在 `max_attempts` 範圍內再試一次。每次 `run` 對每個未成功項目最多新增一次 attempt，不會在單一呼叫內立即用完所有 retries。

變更 reviewer 使用新的 experiment：

```bash
uv run --project benchmarks/aacr --frozen python -m benchmarks.aacr prepare \
  --config /tmp/aacr-config.json --out /tmp/aacr-new-experiment \
  --model fake-reviewer-v2 --reasoning-effort high
```

Judge 可獨立重評：

```bash
uv run --project benchmarks/aacr --frozen python -m benchmarks.aacr evaluate \
  --out /tmp/aacr-experiment --judge-model fake-judge-v2 \
  --judge-reasoning-effort high --judge-rounds 3
```

上述 model 字串在 fake 契約中傳遞，不驗證 provider 是否接受它。未知 reasoning、無效設定、live mode 與執行階段修改 reviewer 設定都會失敗。

## 設定與產物參考

| 欄位 | 契約 |
| --- | --- |
| `mode` | 必須明確為 `fake`；P0–P2 沒有可啟用的 live 路徑 |
| `baseline`、`candidate` | Git commit ref，prepare 解析為完整 SHA；candidate 亦可為 `workspace` |
| `untracked` | 工作區模式要納入的非忽略檔案，逐一列出 |
| `required_files` | 額外必要 workflow 相依檔案，可為共用陣列或按 baseline/candidate 分列 |
| `dataset.kind` | `official` 嚴格比對 upstream lock 的 dataset hash；合成資料用 `fixture` |
| `seed`、`limit` | 固定選樣；先驗證全部原始記錄，無效資料不會因抽樣而消失 |
| `rounds` | reviewer 重跑次數；與 `--judge-rounds` 分開 |
| `timeout` | workflow subprocess 啟動到結束的秒數，包含子代理與工具等待 |
| `max_attempts` | 每個版本／案例／reviewer round 可保存的 attempt 上限 |

`manifest.json` 保存有效模型設定、完整 SHA、snapshot digest、included/excluded 清單、dataset/case hashes、上游版本、工具實作 hash、timeout、concurrency 與 cache policy。目前 concurrency 固定 1，cache 固定 none。`inputs/` 保存 PR Git bundles 與 references，`workflows/` 保存兩版快照，`runs/` 保存每次 attempt，`evaluations/` 保存不可覆寫的各輪評分及報告。

Snapshot 包含 tracked 修改、新加入 index 的檔案、刪除與 executable bits。明列 untracked 檔案必須未被 Git 忽略。工具拒絕 symlink、submodule 與越界路徑，排除已知憑證路徑、執行狀態及 benchmark 產物。兩次讀取與 index/HEAD 檢查不同時，prepare 失敗。必要相依被排除會停止；特殊 workflow 必須用 `required_files` 宣告無法從檔名推導的必要相依。

Artifacts 必須位於來源 worktree 外。每次寫入先驗證 ownership marker、canonical path、manifest hash 與 immutable inputs；工具實作版本不同也拒絕 resume。使用原版本或建立新 experiment，不能改寫舊 manifest。工具不提供自動清理命令；保留資料夾便能重評，刪除資料夾將失去重評與稽核資料。

## 隔離與 fake 契約

每次 attempt 建立獨立 PR clone、Workflow Pack、`HOME`、`CODEX_HOME`、session 與 Base manifest。Base manifest 明確保存空 user summary，Review range 排除 `.scratch/**`，Spec 採 commit-subject fallback。PR 描述不參與選樣或 reviewer 輸入。

Bubblewrap 掛載系統唯讀程式及該 attempt 的 `/work`，建立獨立 PID、network、IPC 與 user namespace。references、Judge 產物、另一版 workflow、使用者 home 與環境密鑰不掛載或繼承。Fake executable 實際嘗試讀取隔離外 sentinel，確認讀取失敗；timeout 回收整個 namespace 的 children。

`fake_codex.py` 接收本機 `codex-cli 0.155.0 exec` 已核對的 argv 形狀，讀取指定 skill、驗證 Base manifest／range／model，並以兩個獨立 fake axis processes 產生 findings。來源 workflow 可帶 `fake-review.json` 指定合成 findings 與故障，這只是測試資料，沒有推論能力。`benchmark.*` events 是明確的 fake instrumentation，**不是已驗證的真實 Codex event API**。缺少 skill、range 或子代理設定證據的輸出會成為 invalid output。

輸出保留 Standards/Spec 分區、原始 JSONL 及每個 finding 的路徑與行號；轉接 scorer 時攤平兩軸並保留原始分區於 evaluation。缺行號保留 null，不猜測位置，也不用模型修復 parse error。零 findings 是成功；missing file、nonzero exit、parse failure 與 timeout 保存不同原因。

P0–P2 不讀真實 vault。設定可提供 `credential_ref: op://vault/item/field`，產生只含 reference 的 env file。離線 fixture 執行與 `op run --env-file <file> -- <command>` 相同的程序邊界，以 `AACR_FAKE_SECRET` 的假值注入 child 的 `CODEX_API_KEY`，不進 argv 或 manifest，保存 log／output 前遮罩。真實 pilot 必須改用既有 `op` wrapper 並驗證授權與遮罩；本版禁止以 fake 模式攜帶真實密鑰。

## 評分與解讀

[vendor/](vendor/) 是 AACR-Bench commit `68a569759289a83654a59d06db2a72910edf0a4a` 的原始 converter、schema、judge、evaluate 與 config，保留 Apache-2.0 LICENSE。每次載入依 [upstream.lock.json](upstream.lock.json) 驗證逐檔 SHA-256。Matching 與 rounding 不修改；離線相容層只替換 semantic judgment 呼叫邊界，明確接收獨立 model/reasoning 並記錄 mock requests。原上游 client 沒有傳遞 reasoning 參數，因此本版不開放其真實模型呼叫。

主要指標是 semantic F1、Precision、Recall、Avg Time 與 Avg Token。前三項差值為百分點；時間與 token 提供絕對差及比例，零分母為 null。官方逐 PR summary、line metrics、matches、原始兩軸 findings 與退步案例都保留。分類未由官方 converter 完整保留，標記 unavailable。

主代理 `self` usage 與子代理相加；`inclusive` 父層 usage 已含子代理，只計一次。Input 已含 cached tokens，output 已含 reasoning tokens，不重複相加。缺 usage 為 unknown，Avg Token 只反映已知範圍，必須搭配 coverage；所有 retries 成本另列。Judge usage 與 reviewer 分開；mock 沒有模型 token 帳單，記為 null。

部分完成的 experiment 標為 incomplete。官方 summary 會排除缺結果，因此這些品質數字只能診斷，不能聲稱完整改善。`experiment_elapsed_seconds` 包含 prepare 至 evaluation 完成的實際經過時間，也包含操作者在命令間的等待；setup、clone、workflow 與 Judge 耗時另列。

多輪 reviewer 與 Judge 結果以 PR 為 cluster 做 paired bootstrap，固定 seed 可重現。小樣本、單輪與同模型 Judge 共同偏差會限制結論；F1 上升不能掩蓋 Precision／Recall 下降，工具不自動判定通過。

## 驗證與後續 pilot

```bash
uv run --project benchmarks/aacr --frozen mypy --config-file benchmarks/aacr/pyproject.toml
uv run --project benchmarks/aacr --frozen python -m unittest discover -s tests -p test_aacr_bench.py -v
uv run --frozen python -m unittest discover -s tests -v
```

後續 5 個 PR × 兩版的真實 pilot 仍需核准費率與預算，並確認 provider model ID、reasoning 傳參、實際 skill 載入、全部子代理 usage、真實 Codex instrumentation、credential references、重試與在途請求成本。描述增強評估另行設計。本輪 fake 成功不代表這些項目已通過。
