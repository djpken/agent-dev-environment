# ADE Plane 到 Linear 同步

KEN-9 的同步由 ADE runtime 啟動，Plane 與 Linear API client 各自維護 provider adapter。Host adapter 不參與同步，也不保存 API credential。

## 啟用

`user.json` 只放 provider 選擇與 grant。先建置 Go CLI、複製範本，再把 `plane-linear-sync` 設為 `true`：

```bash
cp user.example.json user.json
mkdir -p .ade/bin
go build -o .ade/bin/ade ./cmd/ade
.ade/bin/ade plan --user user.json
.ade/bin/ade apply --user user.json --plan-id <plan_id>
```

同步 credential 與連線設定放在 repo 外的 owner-only env file，例如 `~/.config/ade/plane-linear-sync.env`，權限必須是 `0600`：

```text
PLANE_API_URL=https://plane.example.com
PLANE_API_TOKEN=<Plane API token>
PLANE_WORKSPACE_SLUG=<workspace slug>
PLANE_ASSIGNEE_ID=<Plane user id>
LINEAR_API_KEY=<Linear API key>
LINEAR_TEAM_ID=<Linear team id>
```

可選設定包括 `PLANE_ISSUES_URL`、`PLANE_PROJECT_ID`、`LINEAR_API_URL`、`LINEAR_ASSIGNEE_ID`、`LINEAR_PROJECT_ID`、`PLANE_LINEAR_PROJECT_MAP`、`PLANE_LINEAR_STATUS_MAP` 與 `PLANE_LINEAR_TIMEOUT`。未指定 `PLANE_ISSUES_URL` 或 `PLANE_PROJECT_ID` 時，adapter 使用 Plane `/api/v1/workspaces/{workspace_slug}/work-items/advanced-search/`，以 `assignees` filter 查詢 workspace 內的 assigned work items；指定 project 時使用該 project 的 `/work-items/` endpoint 並在 client 端再次確認 assignee。ADE 只把 provider manifest 宣告的環境變數傳給同步程序，不把值寫入 generation、mapping、log 或 command arguments。

手動執行：

```bash
.ade/bin/ade sync --provider plane-linear-sync --env-file ~/.config/ade/plane-linear-sync.env
```

`doctor` 可以檢查 credential 是否已提供，但不會發送 API request：

```bash
.ade/bin/ade doctor --env-file ~/.config/ade/plane-linear-sync.env
```

## 排程

provider manifest 宣告每日三個 local time：`08:00`、`12:00`、`17:00`。ADE runtime 依 host 產生 user-level scheduler 檔案：Linux 使用 systemd user timer，macOS 使用 launchd。排程執行的 command 只包含 env file 路徑，不包含 token。

```bash
.ade/bin/ade schedule install \
  --provider plane-linear-sync \
  --env-file ~/.config/ade/plane-linear-sync.env \
  --enable
```

沒有 `--enable` 時只寫入 scheduler 檔案。`--enable` 才會呼叫 host process manager。時間使用作業系統 local timezone，systemd timer 設定 `Persistent=true`，錯過開機期間的觸發後會補跑一次。

## 欄位與狀態

| Plane | Linear | 規則 |
| --- | --- | --- |
| `name` / `title` | `title` | 保留原始標題 |
| `description` | `description` | 原文後附 Source trace 與穩定 marker |
| `priority` | `priority` | urgent=1、high=2、medium=3、low=4、none=0 |
| `due_date` / `target_date` | `dueDate` | 保留 ISO date |
| `labels` | `labelIds` | 依 team 中完全相同的 label name 對應，找不到的 label 略過 |
| `project` | `projectId` | 使用 `LINEAR_PROJECT_ID` 或 `PLANE_LINEAR_PROJECT_MAP`，原始 project 仍寫入 Source trace |
| state name/group | workflow state | 先套用 `PLANE_LINEAR_STATUS_MAP`，再以同名或 Linear state type 對應 |

Plane 的 completed、closed、done、resolved 會對應 Linear `completed` state；canceled、cancelled、rejected 會對應 `canceled` state；in progress、doing、active 會對應 `started` state；open、todo、unstarted、backlog 會對應 `unstarted` 或 `backlog` state。無法安全判斷時，該筆記錄 mapping error，不用猜測其他 state。

仍指派給使用者的 terminal issue 會同步狀態。解除指派、刪除或從 Plane assigned query 消失的 issue 不會自動關閉、刪除或解除 Linear issue，避免 assigned list 無法區分解除指派與刪除造成破壞性變更。

## Idempotency、追溯與錯誤

mapping 使用 `plane:<workspace>:<Plane issue ID>` 作為 source identity，保存在 `~/.local/share/ade/state/plane-linear-sync/mappings.json`。每次 run 先使用 mapping 更新；mapping 遺失或 Linear target 不存在時，會以 description marker 尋找既有 target，再決定 create。整個 run 取得 local state lock，同一 issue 不會因排程重疊而建立 duplicate。

每次 run 以 JSONL 寫入 `~/.local/share/ade/state/plane-linear-sync/runs/`，包含 run status、created、updated、skipped、每筆 source key 與 error stage。Plane list 失敗會使 run 變成 `failed`；單筆 API、mapping 或欄位 mapping 失敗會保留其他成功項目並回報 `partial`。API credential 會被遮罩，不會出現在 error record。

目前 repo 與 runtime 沒有公司 Plane API credential，因此只執行 fake provider 與 adapter tests；實際 API sync 需使用者在 repo 外提供上述 env file。
