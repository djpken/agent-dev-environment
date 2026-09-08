# agent-dev-environment

[繁體中文](./README.md) · English

A local-first agent development environment for individual developers. This monorepo combines the Workflow Pack with provider management, atomic installation generations, rollback, and Claude Code/Codex/OpenCode workspace adapters.

## ADE runtime

Install only the existing `skills` plugin, or use the full ADE CLI from this repository:

```bash
uv sync --frozen
uv run --frozen ade plan --user user.example.json
uv run --frozen ade apply --user user.example.json --plan-id <plan_id>
uv run --frozen ade doctor
uv run --frozen python -m unittest discover -s tests -v
```

The plan ID covers the bundled workflow contents. Skills and prompts are captured from this repository, while external provider versions remain locked. Model review stays blocked until an explicit endpoint and credentials are configured. Existing skill names and the plugin identity remain unchanged.

See the [runtime guide](./docs/ade-runtime.md), [Plane-to-Linear sync](./docs/ade-plane-linear-sync.md), and [responsibility boundaries](./docs/ade-responsibilities.md). Python runtime code lives in `ade/`; workflows remain in `skills/` and `prompt/`. npm is used only for changesets.

## Module boundaries

| Location | Responsibility |
| --- | --- |
| `skills/`, `prompt/` | Workflow policies and prompts |
| `ade/core.py` | Bundling, composition, plan/apply, and rollback |
| `ade/hosts.py` | Host configuration and workspace attachment |
| `ade/cli.py` | CLI, review adapter, sync entry point, and process lifecycle |
| `ade/sync.py`, `ade/scheduler.py` | Plane-to-Linear reconciliation, local state, and user-level schedules |
| `ade.lock.json`, `ade/provider.schema.json` | Provider versions, capabilities, permissions, and health contracts |
| `tests/` | Runtime, host, sync, and scheduler verification |

## Structure

Skills live under `skills/<bucket>/<skill-name>/SKILL.md`. Buckets:

- `engineering/` — daily code work
- `productivity/` — daily non-code workflow tools
- `misc/` — kept around but rarely used, not promoted
- `personal/` — tied to one's own setup, not promoted
- `in-progress/` — drafts not yet ready to ship
- `deprecated/` — no longer used

`engineering/` and `productivity/` are the **promoted** buckets — every skill in them is registered in this README and in `.claude-plugin/plugin.json`. See `CLAUDE.md` for the full set of conventions.

## Local dev

```bash
scripts/link-skills.sh
```

Symlinks every skill in `skills/` into `~/.claude/skills` and `~/.agents/skills` for local testing. Re-run after adding, removing, or renaming a skill.

## Reference

Skills split on one axis — who can invoke them. **User-invoked** skills are reachable only when you type them; their job is to orchestrate. **Model-invoked** skills can be invoked by you _or_ reached for automatically by the agent when the task fits. A user-invoked skill may invoke model-invoked skills, but never another user-invoked one.

## Existing root-level review workflow

The root-level `SKILL.md` files are the only canonical skill documents and are primarily written in Taiwan Traditional Chinese. This repo does not maintain `SKILL.zh-TW.md` siblings.

- [`/base`](./skills/base/SKILL.md) — Pin the fixed baseline and task source shared by implementation and both review variants.
- [`/code-review`](./skills/code-review/SKILL.md) — Run the stable Standards/Spec range review.
- [`/code-review-ocr`](./skills/code-review-ocr/SKILL.md) — Run the synchronous OCR range review with its own finding counter.
- [`/implement`](./skills/implement/SKILL.md) — Implement the `/base` task source and converge through `/code-review`.

## User-invoked skills

- [`/git-fork-remotes`](./skills/engineering/git-fork-remotes/SKILL.md) — Configure fork/upstream remotes and branch tracking.
- [`/translating-skill-docs`](./skills/productivity/translating-skill-docs/SKILL.md) — Add translated `SKILL.<locale>.md` siblings without changing runtime behavior.
- [`/writing-router-skill`](./skills/engineering/writing-router-skill/SKILL.md) — Ask which skill in this repo fits your situation. A router over the whole skill set.

## Model-invoked skills

- [`/hv-analysis`](./skills/productivity/hv-analysis/SKILL.md) — Run the Horizontal-Vertical Analysis method — a deep-research framework tracing a subject's full history against a same-period competitive comparison — and produce a polished PDF report.
- [`/storage-analyzer`](./skills/productivity/storage-analyzer/SKILL.md) — Read-only macOS/Windows storage analyzer that scans disk usage, tiers cleanup candidates by risk, and generates an interactive HTML report with one-click cleanup.
- [`/neat-freak`](./skills/engineering/neat-freak/SKILL.md) — Knowledge and governance closeout: reconcile project docs, rule files, authorized agent memory, and workspace residue with what the code and runtime actually do.
