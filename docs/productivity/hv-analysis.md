Quickstart:

```bash
npx skills add djpken/agent-dev-environment --skill=hv-analysis
```

```bash
npx skills update hv-analysis
```

[Source](https://github.com/djpken/agent-dev-environment/tree/main/skills/productivity/hv-analysis)

## What it does

hv-analysis runs the Horizontal-Vertical Analysis method: a diachronic axis that traces a subject's full history as a narrative arc, crossed with a synchronic axis that compares it against today's competitors, compiled into a polished PDF research report. The defining constraint is where the value lives — the report doesn't stop at description on either axis; it always crosses the two at the end into new judgment (why history produced today's competitive position, which past bets became today's baggage), not a summary of what came before.

## When to reach for it

Type `/hv-analysis`, or the agent reaches for it automatically when someone asks for a systematic deep dive on a product, company, concept, or person — "研究一下 X", "深度研究", "竞品分析", or a bare product/company name with "帮我研究一下". Reach for this when the ask needs a structured, sourced deep-research report; not for a one-line definition ("XX 是什么"), and not for turning that research into a WeChat article — for that, use the (unpublished, personal-bucket) `khazix-writer` skill instead.

## Prerequisites

The bundled `scripts/md_to_pdf.py` PDF renderer needs WeasyPrint: `pip install weasyprint markdown --break-system-packages`.

## The two axes

**Diachronic** (纵轴) tracks origin through today as a narrative — founding context, key inflection points, the decisions that locked in a direction. **Synchronic** (横轴) holds a single point in time and compares against the current competitive field. Neither axis alone is the point: the report's real value sits where they cross — history explaining today's position, today's position revealing which historical decisions turned into an asset and which turned into a liability.

## Where it fits

A reach-for-it-anytime standalone research skill. Its writing register borrows technique (rhythm, callback, "not officious") from the [khazix-writer](https://github.com/djpken/agent-dev-environment/tree/main/skills/personal/khazix-writer) skill's style guide but stays more structured than a WeChat article — headings and looser colloquialism are both allowed here, unlike in khazix-writer's output.
