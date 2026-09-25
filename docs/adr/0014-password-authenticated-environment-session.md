status: accepted
---

# Password-authenticated environment sessions

ADES uses per-VM username/password accounts for dashboard login. When no
accounts exist, the HTTPS dashboard offers two first-use paths: one-time admin
registration or import of an encrypted WebDAV backup. The first registered
account receives the admin role. Registration closes as soon as an account is
created or imported. Root can still create or rotate accounts with the CLI for
recovery.

Passwords are stored as salted scrypt hashes in the root-owned authorization
directory. Bearer session tokens remain hashed in private session state and
expire after twelve hours. Updating an account invalidates its active sessions
and pending manual approvals. Existing wallet sessions are not migrated.
Wallet allowlist data remains only for validating legacy signed update
policies; wallet identity cannot create dashboard sessions.

Admins can upload an encrypted environment backup to a full WebDAV file URL.
The versioned bundle contains the ADES manifest, account hashes, and effective
update policy. It uses scrypt with N=32768, r=8, p=1 to derive an AES-256-GCM
key. The backup passphrase must contain at least twelve bytes. WebDAV access
uses HTTPS; optional Basic Auth credentials are entered for each operation and
are never saved in ADES or included in the bundle. The dashboard confirms
before overwriting the destination file.

Import is available only while the local account store is empty. It accepts a
backup with the same VM ID, source root, and state root, validates the manifest
and account data, preserves local file ownership and permissions, and clears
all existing sessions and approvals. A changed manifest triggers a delayed
systemd restart so the service applies the restored settings. Session state,
run history, WebDAV credentials, TLS certificate files, and TLS private keys are
outside the backup.

Password and WebDAV credentials require direct TLS or a trusted reverse proxy
connection marked HTTPS from loopback. Remote forwarded-protocol headers do
not establish transport security. Login and registration fields use standard
autocomplete values for browser password managers, including 1Password.

An admin session can replace a legacy scheduled policy with a root-owned
policy, including a disabled policy that prevents an older shared policy from
reactivating. The one-use run approval, fixed component allowlist, and
root-owned updater boundaries remain in force.

## Consequences

- The first HTTPS visitor can claim the one-time admin registration while the
  account store is empty. Operators can instead restore an encrypted backup.
- Root can create or rotate accounts with:

  ```bash
  sudo /usr/local/bin/ade environment account set --username <name> --role admin
  ```

  The CLI reads the password without terminal echo.
- Backup import is a same-VM restore. Moving settings to another VM requires a
  separately prepared manifest and account setup.
- Passwords and WebDAV credentials are never passed in command arguments or
  environment variables. They travel in HTTPS request bodies or headers and
  are not logged by ADES.
- The dashboard has no wallet login or wallet registration flow.
