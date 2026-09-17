---
status: accepted
---

# MetaMask wallet-signed environment control

The per-operation signing and expiring-policy decisions below are superseded by
[ADR-0013](0013-wallet-authorized-environment-session.md). The MetaMask identity,
registration, HTTP opt-in, and anonymous-data boundaries remain applicable.

The environment management surface uses MetaMask Ethereum accounts and EIP-191
`personal_sign` for first-wallet registration, session authentication, individual
control requests, and signed update policies. Root helpers independently verify
the signatures; the VM stores public addresses only. The 2026-09-16 decision
replaces Solana/Phantom because the operator needs direct HTTP access through
both a company VPN and Tailscale. The existing ADR filename remains stable for links.

HTTPS remains the installer default. An operator can explicitly enable a remote
HTTP listener with `allow_http: true` and an empty `tls` object. HTTP relies on
the surrounding network for transport protection; signatures do not protect
page delivery or bearer-session confidentiality. The service retains signed
controls and never interprets HTTP access as authorization to update components.

Login signs an exact application-specific, VM-bound challenge with the selected
allowlisted origin, nonce, and expiry. This is not SIWE; the previous permissive
SIWS substring matcher is removed. IP and Tailscale origins must be individually
configured. Accounts are normalized to lowercase Ethereum addresses. Contract
wallets requiring ERC-1271 are outside this implementation; ordinary MetaMask
accounts require no RPC connection, token balance, or transaction fee.

The first wallet signs its one-time registration message and becomes admin.
Later identity changes remain explicit root-owned enrollment operations.
Existing Solana addresses and policies require intentional re-enrollment; they
are not automatically converted. Missing or invalid update policies block
scheduled updates.

All environment information requires an authenticated wallet session, including
health checks and registration status. Anonymous clients receive only the empty
login shell and the challenge/verification endpoints required for authentication
and initial enrollment. Bootstrap signatures bind an opaque VM digest without
revealing its internal identifier. Sign-out revokes the bearer session; account
changes, disconnects, and expired sessions clear the dashboard, and stale async
responses cannot repopulate it after sign-out.
