# RTK - Rust Token Killer

**用途**：壓縮支援指令的輸出；實際效益用 `rtk gain` 查詢，不代表整段任務的總 token 節省。

支援的指令使用 `rtk <command>`；不支援或需完整原文時用 `rtk proxy <command>`。`proxy` 不過濾輸出。

## Meta 指令（永遠直接用 rtk）

```bash
rtk gain              # 顯示 token 節省分析
rtk gain --history    # 顯示指令使用歷史與節省量
rtk discover          # 分析 Claude Code 歷史紀錄，找出漏掉的最佳化機會
rtk proxy <cmd>       # 執行未過濾的原始指令（供除錯用）
```

## 安裝驗證

```bash
rtk --version         # 應顯示：rtk X.Y.Z
rtk gain              # 應能正常執行（不是 "command not found"）
which rtk             # 確認是正確的執行檔
```

⚠️ **名稱衝突**：如果 `rtk gain` 失敗，可能裝到 reachingforthejack/rtk（Rust Type Kit）而不是這個。
