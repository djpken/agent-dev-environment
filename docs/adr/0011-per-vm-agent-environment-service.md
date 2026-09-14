---
status: accepted
---

# Per-VM Agent environment service

Each supported VM runs an independent `Agent environment service` as its management plane for health, allowlisted component lifecycle, manual controls, and scheduled updates. The service owns live dashboard and JSON API access; the existing `Web artifact publisher` remains a separate publication boundary for agent-created public content and optional read-only environment snapshots. This separation keeps management operations out of anonymous artifact content and lets the same service operate on VMs that do not host the artifact publisher.

## Considered options

- Reuse the artifact publisher route: rejected because static public content is not a lifecycle or authorization boundary.
- Put the manager in Orca automation: rejected because a manager must survive an Orca restart and must be able to coordinate root-owned service updates.

## Consequences

- VM recipes install and configure one manager per VM.
- Component commands remain root-owned manifest data and are never supplied by HTTP callers.
- A VM can expose health without enabling public snapshots or the artifact publisher.
