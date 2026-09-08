# ADR-0007：以宣告式更新與明確 lifecycle 管理 ADE

- 狀態：接受
- 日期：2026-09-04

ADE 採宣告式 lockfile。安裝或更新先產生變更計畫，驗證後原子切換 ADE 管理的版本，保留上一版供 rollback。版本 rollback 不代表 provider 資料遷移可逆；不可逆遷移必須獨立揭露與處理。

一般設定依 `ADE defaults < user config < workspace config` 合併。Workspace 可以選擇 workflow 與申請 provider，但不能自行提高權限或改變資料出口；有效權限不得超過使用者授權。此限制須由安裝與執行元件落實，prompt 只描述政策。

Headroom 列為可選 Capability provider，預設關閉。啟用範圍限定到使用者選定的 host 或 profile，遵守 ADR-0005 的 local-first 邊界；context 改寫需要可停用，以利除錯。

Provider manifest 宣告 `host-spawned` 或 `shared-local` lifecycle。前者由 host 啟動與結束；後者由 ADE runtime 管理啟動、健康檢查與停止，Host adapter 只負責連接。Provider 擁有自己的業務狀態，啟動元件負責 process lifecycle。這個分工允許 stdio 工具與共享本機服務共存，避免多個 host 同時管理同一 process。
