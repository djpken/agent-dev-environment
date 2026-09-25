---
status: superseded
---

# Wallet-authorized environment sessions

Superseded by [ADR-0014](0014-password-authenticated-environment-session.md),
which replaces wallet login and wallet-owned identities with root-provisioned
username/password accounts.

Every operation permitted by the wallet's role, including component updates,
restarts, and update-policy changes, uses one twelve-hour bearer session without
another wallet signature. This replaces the per-operation signing requirement
in ADR-0012. Registration and login still prove wallet ownership. The dashboard
keeps the session in sessionStorage and validates it on reload; expiry requires
a new login. There is no automatic renewal or additional refresh token.

The root-owned authorization helper creates challenges, verifies the exact
VM/origin-bound login message, and stores only hashes of bearer tokens. It checks
both the role at login and the current allowlist, so an existing session cannot
gain permissions after a role upgrade. Session state persists across service
restarts. Sign-out, expiry, or removing the wallet blocks further interactive
requests and pending manual executions. Work already executing may finish.

The unprivileged HTTP process cannot approve its own work: the helper records a
one-use approval binding the run ID, trigger, action, components, exact target
versions, and requester. The root updater checks that approval and the live
session before executing manifest-owned commands, including manifest dependencies.
The privileged invocation also fixes the expected trigger. Keeping these records
beside the root-owned config prevents writable run history from becoming authority.
The source checkout executed by root helpers remains trusted deployment code.

An admin session can enable, replace, or disable a persistent update policy.
Policies have no expiry and continue after sign-out, session expiry, or changes
to the original author's identity. Execution still checks the current VM and
component allowlist. The root-owned policy is authoritative; shared run storage
cannot replace or reactivate it. This separates interactive session lifetime
from the user's explicit decision that scheduled work must continue until stopped.

Existing individually signed policies retain their original scope and expiry
until replaced from an admin session. Deployments must install the new helper
and sudoers entry before restarting the dashboard service. Old in-memory sessions
are not migrated. HTTP opt-in retains ADR-0012's dependence on the surrounding
network for page integrity and bearer-token confidentiality.
