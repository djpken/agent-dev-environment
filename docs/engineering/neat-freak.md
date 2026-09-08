Quickstart:

```bash
npx skills add djpken/agent-dev-environment --skill=neat-freak
```

```bash
npx skills update neat-freak
```

[Source](https://github.com/djpken/agent-dev-environment/tree/main/skills/engineering/neat-freak)

## What it does

neat-freak is a knowledge-and-governance closeout: it reconciles project docs, rule files (CLAUDE.md/AGENTS.md), authorized agent memory, and leftover workspace residue against what the code and runtime actually do, so the next session — or the next person — finds one current answer instead of several stale ones. The defining constraint: it only ever expands how deeply it checks, never what it's allowed to write — destructive cleanup (branches, worktrees, temp repos) always stops for an explicit user confirmation delivered after a full report, and never folds into the same turn as the original request even if that request said "clean up when done."

## When to reach for it

Type `/neat-freak` (or say "洁癖" / "/neat"), or the agent reaches for it automatically at a natural closeout point — syncing docs, rules, and memory after a feature lands, a stale or conflicting CLAUDE.md, or handing a project to a fresh session or a teammate. Reach for this at knowledge-closeout moments; not for plain coding, refactoring, or debugging work, not for tidying non-project prose (JSON, a weekly report, changelog copy), and not for a bare "整理" with no project-knowledge context behind it.

## Prerequisites

Needs filesystem read access. `scripts/audit-inventory.sh` needs Bash for the fast read-only inventory pass — without Bash, do the equivalent checks by hand. Git and `rg` sharpen verification but aren't required. Writes and any destructive action always follow whatever authorization rules the active agent, workspace, and user already have in place — this skill never grants itself more.

## Light path vs. full path

Most personal, single-maintainer projects take the **light path**: inventory → align facts against current code → backfill a minimal rule file if none exists → sweep session residue (stale PLAN.md/TODO.md, replaced `_old`/`_backup` copies) → report. Projects with a real release process, remote collaboration, or multi-platform state take the **full path** (steps 0–7 in `SKILL.md`), which adds a live fact matrix per knowledge surface (code / runtime / docs / rules / memory / workspace), a rule-governance audit, and a two-phase report that separates what already changed from what still needs the user's call before anything gets deleted.

## It's working if

- Every relevant knowledge surface ends the run tagged `verified-current`, `changed-and-verified`, `pending`, `out-of-scope`, or `not-applicable` — never silently skipped.
- The report's deletion candidates are listed and confirmed by the user before anything is removed, even when the original ask said "clean up after."
- No fact got copied into a second authoritative location — each stays a single source of truth, with pointers elsewhere instead of duplicates.

## Where it fits

A reach-for-it-anytime standalone skill, most natural at the end of a dev session, before a handoff, or right after a release.
