# User-level `tufantunc/ssh-mcp` selection

- 狀態：接受
- 日期：2026-09-14

本 repo 記錄選用 [`tufantunc/ssh-mcp`](https://github.com/tufantunc/ssh-mcp) `2.8.1` 作為個人 user-level Codex MCP server；實際 entry 放在 Codex 有效的 `config.toml`，也就是 `$CODEX_HOME/config.toml`，未設定時為 `~/.codex/config.toml`。SSH profiles 與 credential 放在各 VM 的 `~/.config/ssh-mcp/config.toml` 和環境變數。選用理由是一般遠端伺服器管理涵蓋 SSH/SFTP、命令 policy、approval、audit log、跨平台設定與近期 release/CI；repo 不承擔 project-level provider registration，避免把使用者的遠端環境與 credential 邊界帶入共享設定。

## Considered Options

- project-level `.mcp.json`：會讓遠端能力與 project checkout 綁定，也容易把不同 VM 的 host/profile 差異誤當成共享設定。
- ADE lockfile provider：適合由 ADE composition repository 擁有的 provider manifest；本次需求是個人 user-level 工具，不納入所有 workspace 的 provider 清單。
- `bvisible/mcp-ssh-manager`：安全 policy 與 DevOps tools 很完整，但近期 release 曾修復多個 command-injection advisory，保留為替代方案。
- `gelse/ssh-mcp`：集中式 HTTP gateway 適合多 agent，與目前個人 stdio 使用情境不同。

## Consequences

- 每台 VM 都要在 user-level Codex config 啟用固定版本 `ssh-mcp@2.8.1`，並自行提供 SSH profile、host key 與 credential。
- repo 不會自動替使用者選擇遠端 host，也不會提交 `.mcp.json`、SSH TOML 或 secrets。
- `ssh-mcp --version` 會啟動 stdio server，不能當作版本 health check；user-level Codex entry 不依賴 ADE health command。
