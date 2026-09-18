# Codex 效率改善：KEN-14／15／19

## 目標與判定

2026-09-17 與使用者確認：先改善個人 Codex 開發流程，在品質與必要驗證不退步的前提下減少 token 及完成時間。先比較原生功能與既有指示，再決定是否加入外部工具。

採用門檻為三次完整試驗的總 token 中位數降低至少 15%，耗時中位數增加不超過 10%，且每次品質檢查皆通過。缺少 usage、執行失敗或品質未過都不能視為節省。合格組合依 token 中位數、耗時中位數排序；沒有合格組合就保留個人預設。

## 實測結果：2026-09-18 完成

12 個完整 trials、36 輪對話的品質檢查全部通過，但沒有組合達到 token 門檻。個人 context management 預設維持關閉，沒有用合併 prompt 取代全域 AGENTS.md。

| 組合 | 總 token 中位數 | 相對基準變化 | 耗時中位數 | 相對基準變化 | 品質 |
| --- | ---: | ---: | ---: | ---: | --- |
| Baseline | 412,409 | 基準 | 116.0 秒 | 基準 | 3/3 通過 |
| 原生 context management | 417,846 | +1.32% | 111.6 秒 | -3.77% | 3/3 通過 |
| 精簡 repo 指示 | 413,920 | +0.37% | 116.9 秒 | +0.78% | 3/3 通過 |
| 兩者合用 | 408,259 | -1.01% | 117.8 秒 | +1.56% | 3/3 通過 |

正值代表增加。合併組有一次使用 303,772 tokens，但三次中位數只減少 1.01%，不能挑選最好的一次宣稱有效。Baseline 與候選組都仍出現整份 log 輸出後再查錯誤的工具呼叫；部分回合還先呼叫本機不存在的 `python`，再改成 `python3`。這些實際工具行為的成本抵銷了文字縮減。

Root AGENTS.md 從 5,129 減為 1,513 字元，減少 70.5%；合併 prompt 從 8,394 減為 4,376 字元，減少 47.9%。這是字元長度，不是模型 token 或帳單節省。repo diff 保留為可審閱候選；未宣稱已達效率目標，也未據此套用個人預設。

量測明細見 [不含原始對話的 JSON 紀錄](codex-efficiency-results.json)。正式比較結束後重新解析全部 usage、重跑獨立程式碼檢查，另確認沒有新增非預期檔案或引入非標準函式庫。候選檔案的 SHA-256 全程固定；從個人設定移除本次自動產生的 trial project 區塊並正規化尾端空白後，能重建試驗起點的 SHA-256，證明正式比較期間其他設定沒有漂移。

### 回覆規則與收尾驗證

- `python3 -m unittest discover -s tests -p test_codex_efficiency.py -v`：13 項通過，涵蓋 usage 完整性、resume 累計值、快取子集合、品質／耗時／樣本門檻、逾時與 trust 清理。
- `python3 scripts/prompts.py check`：兩份合併檔一致。
- 兩版合併 prompt 在相同全域指示下各跑六個回覆情境：JSON-only、已知錯誤、預設繁中、英文 commit、部署／rollback 必要細節、破壞性操作的確認。人工複核後兩版皆保留全部要求，且沒有工具呼叫；這是回覆契約檢查，不是效率採用樣本。[情境與回覆紀錄](codex-prompt-contracts.json)
- 初步字串檢查因兩版都把 `nullable` 正確表達為「允許 NULL」而誤報。紀錄保留原始字串結果及語意複核，沒有修改模型輸出來製造通過。
- 已移除本次 pilot 與正式試驗共 14 個臨時 trusted project 紀錄，逐項確認其值仍是 Codex 產生的原樣，並驗證其他設定未受清理影響。個人 context management 欄位仍未新增，維持關閉。

## 確認的環境與變更

- 本機為 `codex-cli 0.154.0`，模型 `gpt-6-astra`，reasoning `medium`，使用既有 ChatGPT 登入；沒有複製或讀出憑證。
- 開始查核時，個人設定與 Orca runtime-home 的設定內容相同；runtime-home 的 AGENTS.md 指向個人 AGENTS.md。原先 `context_management` 顯示為 `false`。
- 單次 override `features.context_management.experimental_mode=true` 後，`codex features list` 顯示 `context_management=true`，模型請求成功並回傳 usage。這只證明此版本接受設定且可完成請求，不代表已驗證長歷史搜尋或 compaction 的品質。
- 現行個人 AGENTS.md 並未直接載入 repo 的 `prompt/prompt.md`。兩者分開評估；合併 prompt 的縮短不能計為現行 Codex session 已節省的 token。
- 根目錄 AGENTS.md 將 Workflow Pack 維護規則與完整圖譜證據規則移至有明確觸發條件的文件，保留 runtime 驗證、ownership、搜尋選擇與覆蓋範圍要求。另加入縮小查詢範圍、避免重讀及先篩選大量輸出的規則。
- ADHD、Caveman、Ponytail 合併重複敘述，保留繁中、ultra、Auto-Clarity、必要細節、執行驗證、安全界線與退出語句。RTK 移除未經本地驗證的固定節省比例；圖譜 prompt 補齊既有 repo 的證據與降級政策。1Password prompt 未修改。
- 全域安裝目錄下查無指向本 repo 的 skill 連結；本 repo 的兩個 `.agents/skills` descriptions 已相當短。此次未改外部安裝的 skills，也未改 promoted skill 行為或 registry。

原生功能的正式設定、登入方案限制見 [OpenAI configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)。指示精簡的來源為 [Rethinking skills and prompts for GPT-6 Astra](https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra)，不據此推論固定節省幅度。

## 可重現的對照

```bash
python3 scripts/codex_efficiency.py \
  --output /tmp/ade-codex-efficiency-comparison \
  --revision ef64277cefd62245556d96c23c1171d4743f0d4e \
  --model gpt-6-astra --reasoning medium --repeats 3
```

需使用尚不存在、位於 repo 外的 output 目錄。腳本使用標準函式庫、PATH 上的 `codex` 與既有驗證設定；不自動安裝 provider、啟用個人 feature 預設或提交程式碼。Codex 會自動登錄 trial 工作目錄為 trusted project；腳本收尾只移除本次目錄的原樣紀錄，遇到同期修改則拒絕覆寫。原始 responses 留在權限受限的本機目錄，只有不含對話內容的 summary 適合加入研究紀錄。Codex 的續接 session 也會由 Codex 自行保存在它的 session store。

四組依序為 baseline、context、instructions、combined，每輪輪替順序，不平行執行計時樣本。各組使用同一個 `git archive` snapshot，排除工作區其他尚未提交的環境管理變更；instructions 組只覆蓋 root AGENTS.md 與兩份按需讀取的參考文件。個人指示、skills 與模型保持相同。

每個 trial 包含四類任務、三輪對話：

1. 唯讀查核：精確查找 Headroom 設定、跨檔案核對 Codex host export，並從 6,000 筆成功 log 與三筆失敗紀錄中找出全部錯誤。
2. 小型修改：實作 `summarize(records)`，遵守上一輪的標準函式庫、固定輸出 keys 與不修改輸入等約束。
3. 延續修改：處理缺少 `status` 的資料，保留既有約束並重新驗證。

查核使用具體欄位與實際 `path:line` 證據；父程序獨立驗證空輸入、mixed records、缺少欄位、輸入不變與既有 repo 檔案未被修改。比較期間所有組別停用外部 MCP，要求明示原始碼降級驗證，因此這是受控程式碼／log 任務測量，不能外推為 MCP 圖譜或所有日常工作流程的成效。沒有壓力觸發 compaction 的保證。

`turn.completed.usage` 使用實際 input、cached input、output 計數。[Codex 0.154.0 的實作](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/exec/src/event_processor_with_jsonl_output.rs) 使用 thread cumulative totals，因此跨輪取最後一筆並驗證單調遞增，不把各輪累計值相加；cached input 是 input 的子集合，不重複加總。總 token 為 input + output，另列 cached input，不能把總 token 降幅直接稱為帳單降幅。

第一次 pilot 發現問題描述對 config 路徑與 namespace 格式有歧義，且早期計數器錯把 session 累計值相加。已修正提示與計數，pilot 全部排除於正式比較之外。

## 8 張 issues 的取捨

| Issue | 決策 |
| --- | --- |
| [KEN-15](https://linear.app/ken-hsu/issue/KEN-15) | 原生 context management 單獨比較；設定接受與模型回覆成功不等同品質／效率驗收。 |
| [KEN-19](https://linear.app/ken-hsu/issue/KEN-19) | 第一批實作指示精簡與按需讀取，透過實際 usage 評估。 |
| [KEN-14](https://linear.app/ken-hsu/issue/KEN-14) | 評估 ADHD 指示的本地改編；「排名第一」缺少榜單與觀察時間，保留未解，不以人氣當效益證據。 |
| [KEN-6](https://linear.app/ken-hsu/issue/KEN-6) | 沿用現有 ownership：Workflow Pack 管指示、host 管原生 context 功能、runtime 管 provider 安裝與 lifecycle。不建立第二套 session memory。 |
| [KEN-7](https://linear.app/ken-hsu/issue/KEN-7) | 維持 zvec-grep integration-only candidate。Linear 留言中的舊研究檔在目前 checkout 不存在；舊稱「沒有 runtime registry」也須重新核對，不能直接沿用整份舊結論。 |
| [KEN-16](https://linear.app/ken-hsu/issue/KEN-16) | context-mode 留作大量輸出隔離的下一批候選。先不與原生功能或 Headroom 疊加，以免無法辨識效果來源。 |
| [KEN-17](https://linear.app/ken-hsu/issue/KEN-17) | 瀏覽器比較延後；先明確定義代表性網頁任務，再比較既有 Orca 能力與候選工具。 |
| [KEN-18](https://linear.app/ken-hsu/issue/KEN-18) | contact sheet 延後至視覺任務試驗，需檢查小字與細節辨識是否退步。 |

外部工具已完成文件與介面初查，尚未在本 VM 安裝或測量：

- [zvec-grep MCP](https://github.com/zvec-ai/zvec-grep/blob/e76c89f1e0d0577713fa74b67acc882343fe982d/docs/03-mcp.md) 提供語意檢索；精確搜尋仍留給 `rg`，排名結果不取代圖譜的呼叫鏈與涵蓋範圍證據。索引與冷啟動成本須單獨記錄。
- [context-mode Codex adapter](https://github.com/mksglu/context-mode/blob/8853c3cedae76b5c3325b41fb363c1cd8cf7fbca/src/adapters/codex/index.ts) 的 `canModifyArgs` 與 `canModifyOutput` 為 false。MCP 可用不代表所有 Codex 工具輸出自動被壓縮；整份 upstream routing 不宜覆蓋 ADE 的搜尋政策。
- [context-mode benchmark](https://github.com/mksglu/context-mode/blob/8853c3cedae76b5c3325b41fb363c1cd8cf7fbca/BENCHMARK.md) 的輸出體積縮減不等於 Codex 整段任務 token 節省；本次不採用其宣傳數字作為驗收結果。
- ADE lockfile 的 Headroom 固定為 `0.36.5`、預設關閉；不能把 upstream main 的新功能視為此固定版本已有的能力。

## 個人設定套用與回復原則

只有完整品質與數值門檻通過，才考慮套用測得的組合。repo 的工作區 diff 是可審閱候選，個人設定另外處理；合併 prompt 未經載入試驗不能直接取代全域 AGENTS.md。

若套用原生功能，只修改既有 TOML 中 `features.context_management.experimental_mode`，先保存該欄位／表格原值並確認設定未被同期修改。回復只還原本次欄位，保留其他個人設定與同期變更。此次未新增 ADE CLI、provider API 或設定 schema。
