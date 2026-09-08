Quickstart:

```bash
npx skills add djpken/agent-dev-environment --skill=storage-analyzer
```

```bash
npx skills update storage-analyzer
```

[Source](https://github.com/djpken/agent-dev-environment/tree/main/skills/productivity/storage-analyzer)

## What it does

storage-analyzer runs a read-only disk-usage scan (macOS or Windows, auto-detected) and turns it into an interactive HTML report that tiers every space hog into 🟢 auto-cleanable, 🟡 needs a human call, or 🔴 handle-with-care, each with a concrete disposal path. The defining constraint: the agent's own shell session never deletes, moves, or empties anything — every scan step is read-only (`df`/`du`/`diskutil`/`stat`/`ls`), and the only place a file can actually be removed is inside the generated report itself, where the user clicks a button in the browser and confirms.

## When to reach for it

Type `/storage-analyzer`, or the agent reaches for it automatically when someone says their disk or storage is full, asks what's eating space, or wants cleanup suggestions. Reach for this for storage/disk-space questions; for RAM or process-memory questions ("哪个进程吃内存", "内存占用高") this doesn't apply — that's Activity Monitor / Task Manager territory, a different resource entirely.

## Prerequisites

None beyond Python 3 — every script (`scan.py`, `build_report.py`, `server.py`) is standard-library only, zero third-party dependencies. Windows doesn't ship Python, so it needs a one-time install there; macOS ships everything it needs out of the box.

## The three-tier report

🟢/🟡/🔴 works like a traffic light on a *cleanup decision*, not a full disk inventory: 🟢 is safe to auto-clean (cache, temp, reinstallable dev caches), 🟡 needs a person to look because it might hold real user data, 🔴 is stuff you might want gone but shouldn't hand-delete — it gets an uninstall path, never a delete button. Routine files with no real "should I remove this?" decision skip the tiers entirely and fall into the neutral "system & other" band of the disk bar instead of forcing a verdict on everything.

## It's working if

- The report opens through `server.py` (interactive, one-click) by default; the static `build_report.py` output only appears when the user explicitly wanted a shareable read-only file.
- Every 🟢 item carries `trash_paths`; every 🔴 item gets a concrete uninstall path instead of a delete button.
- The conversation summary leads with the total reclaimable estimate and the top 2–3 items to clean first, leaving detail to the report.

## Where it fits

A reach-for-it-anytime standalone skill.
