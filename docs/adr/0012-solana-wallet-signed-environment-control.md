---
status: accepted
---

# Solana wallet-signed environment control

The environment management surface permits public read-only health access and protects state-changing operations with Solana Wallet Standard authentication and Ed25519 signatures. `solana:signIn` establishes a browser session, `solana:signMessage` authorizes a specific control request, and a separately signed `Update policy` authorizes unattended daily updates. The service uses HTTPS, binds each request to a VM identity and nonce, and stores no wallet private key.

## Considered options

- Wallet-held private keys on the VM: rejected because a VM compromise would expose the operator identity.
- Unsigned HTTP controls: rejected because wallet signatures do not protect the management page from transport or script tampering.
- On-chain SOL transactions: rejected because maintenance authorization needs off-chain signatures and no transaction fee or chain state.

## Consequences

- Every VM needs an explicit authorized Solana wallet enrollment.
- The manager needs a trusted HTTPS origin before browser wallet controls are enabled.
- Scheduled updates stop applying when the signed policy is missing, expired, revoked, or outside the component allowlist.
