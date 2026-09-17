# User-level SSH MCP

本 repo 只記錄 [`tufantunc/ssh-mcp`](https://github.com/tufantunc/ssh-mcp) `2.8.1` 的技術選型，詳見 [ADR-0011](./adr/0011-user-level-ssh-mcp-provider.md)。實際 MCP entry 放在 Codex 有效的 `$CODEX_HOME/config.toml`，未設定 `CODEX_HOME` 時使用 `~/.codex/config.toml`；SSH profiles 放在 `~/.config/ssh-mcp/config.toml`。

## Codex user-level entry

在有效的 Codex `config.toml` 加入：

```toml
[mcp_servers.ssh-mcp]
command = "npx"
args = ["--yes", "ssh-mcp@2.8.1"]
env_vars = ["SSH_AUTH_SOCK", "SSH_MCP_PASSWORD", "SSH_MCP_KEY", "SSH_MCP_PASSPHRASE", "SSH_MCP_SUDO_PASSWORD"]
```

也可以用 `codex mcp add ssh-mcp -- npx --yes ssh-mcp@2.8.1` 建立 entry，再補上 `env_vars`。用 `codex mcp list` 確認 entry 已啟用。

## SSH user-level profile

上游預設讀取：

- Linux：`~/.config/ssh-mcp/config.toml`
- macOS：`~/Library/Application Support/ssh-mcp/config.toml`
- Windows：`%APPDATA%\\ssh-mcp\\config.toml`

最小安全設定：

```toml
[defaults]
defaultProfile = "dev"
approvalMode = "ask-destructive"

[[profiles]]
name = "dev"
host = "REPLACE_WITH_SSH_HOST"
port = 22
user = "REPLACE_WITH_SSH_USER"
auth = "agent"
role = "operator"
readOnly = false
approvalPolicy = "ask-all"
```

設定檔必須只有 user 可讀：

```bash
mkdir -p "$HOME/.config/ssh-mcp"
chmod 700 "$HOME/.config/ssh-mcp"
chmod 600 "$HOME/.config/ssh-mcp/config.toml"
```

使用 key 或 password 時，把 profile 的 `auth` 改成 `key` 或 `password`，credential 只放在 VM 的 `SSH_MCP_KEY`、`SSH_MCP_PASSWORD` 或 `SSH_MCP_PASSPHRASE` 環境變數。不要把 secrets 寫入 repo 或 command arguments。正式環境使用非 root 帳號、確認 host key，並保留人工 approval。
