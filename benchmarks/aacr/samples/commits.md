# AACR workflow comparison

**smoke-only · report-only**

Status: complete. offline pipeline evidence only; no model quality or merge verdict

| Metric | Baseline | Candidate | Difference |
| --- | ---: | ---: | ---: |
| semantic_f1 | 0.0 | 1.0 | 100.0 |
| precision | 0.0 | 1.0 | 100.0 |
| recall | 0.0 | 1.0 | 100.0 |
| avg_time | 0.3160973209887743 | 0.3163263197056949 | 0.00022899871692061424 |
| avg_tokens | 180 | 180 | 0 |

Quality differences are percentage points. Full ratios, usage coverage, attempts,
line metrics, matches and two-axis provenance are in report.json.

Requested/completed: baseline 1/1; candidate 1/1.

- Mock output is smoke-only; real model, skill loading and all child usage need pilot validation.
- Spec uses commit subjects; original requirements are limited.
- Same-model reviewer/Judge may share bias. Tokens are not billing amounts.
- Small samples and single rounds cannot establish significance.
- Unknown usage forbids complete cost-improvement claims.
- Incomplete runs are diagnostic only; official summaries omit missing results.
- Classification unavailable in the official converted schema.
