# ADR-0009：以 ADE runtime 管理 Plane 到 Linear 的單向同步

狀態：accepted

Plane 是來源，Linear 是個人追蹤目標。ADE runtime 擁有三次每日排程、local state lock、Plane issue ID mapping 與 audit；Plane/Linear adapter 只負責 API，sync engine 負責欄位與狀態 mapping。mapping 以 workspace 加 Plane issue ID 為穩定身份，並在 Linear description 保存 source marker 來處理本機 mapping 遺失或 create 後中斷。解除指派不會自動關閉或刪除 Linear issue，因為 assigned-only API 回應無法安全區分解除指派、刪除與查詢缺頁；這個保守選擇避免破壞性同步，代價是 Linear 可能保留已解除指派的工作項目。

credential 只從 process environment 或 repo 外的 owner-only env file 讀取。排程只記錄 env file path，local state 與 audit 也不保存 credential。
