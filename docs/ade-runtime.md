# ADE runtime

ADE runtime 是 local-first 的 Python CLI。Workflow Pack 由 `skills/` 與 `prompt/` 提供；`ade/` 管理安裝、provider grant、host attach、sync 與 user-level schedule。Python dependencies 與版本由 `uv.lock` 固定。

## 常用指令

```bash
uv sync --frozen
uv run --frozen ade plan --user user.example.json
uv run --frozen ade apply --user user.example.json --plan-id <plan_id>
uv run --frozen ade doctor
uv run --frozen ade attach --host codex --workspace /path/to/project --apply
```

`user.json`、credential env file 與 ADE state 都在 repo 外或 `.gitignore` 內。Workspace config 只能選 provider 與 prompt，不能提高 grant 或改寫 data egress。Provider command 以 argv 啟動，不經 shell；host-spawned provider 由 host 擁有 process，shared-local provider 由 ADE foreground supervisor 擁有 process。

## Plane 到 Linear

啟用 `plane-linear-sync` 後，用 repo 外的 owner-only env file 手動執行：

```bash
uv run --frozen ade sync --provider plane-linear-sync --env-file ~/.config/ade/plane-linear-sync.env
uv run --frozen ade schedule install --provider plane-linear-sync --env-file ~/.config/ade/plane-linear-sync.env --enable
```

排程由 `ade/scheduler.py` 渲染 systemd user timer 或 macOS launchd，時間固定為 `08:00`、`12:00`、`17:00` 的 local timezone。排程 command 只包含 env file path，不含 token。同步的 mapping 與 JSONL run record 位於 `~/.local/share/ade/state/plane-linear-sync/`，檔案權限由 runtime 設為 owner-only。

完整欄位、狀態、credential 與錯誤規則見 [ADE Plane 到 Linear 同步](./ade-plane-linear-sync.md)。
