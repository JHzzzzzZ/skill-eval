# skill-evaluator

A skill that evaluates other skills — against 19 quality metrics, producing a machine-readable `report.json` and a human-readable `report.md`.

It is itself a skill (for [pi](https://github.com/earendil-works/pi-coding-agent)), not a web service: copy it into your skills directory, point it at any skill package, and run it.

## What it checks

19 metrics are grouped by **measurement method**, not run one-by-one:

| Method | Metrics | Tooling |
|---|---|---|
| Static analysis | #2 name/description rules & token budget, #7 invocation mode, #13 dangerous-command scan | `scripts/static_check.py` |
| Sandbox runs | #1 trigger precision/recall/F1, #4 cost stats, #5 necessity A/B, #8 minimal deps, #9 result verification, #10 process audit, #12 stability, #15 idempotency | `scripts/trace_run.py`, `score.py`, `trigger_judge.py`, `idem.py`, `compare.py`, `process.py` |
| LLM review | #3 brevity, #6 redundancy, #11 fallback, #16 pre-check, #17/18 I/O contract, #19 side-effect reversibility | `judges/*.md` rubrics + `judge_runner.py` |
| Historical diff | #14 version evolution | `scripts/evolution.py` |

Several metrics share one sandbox run — see `skill-evaluator/reference.md` (§ Run Scenarios) for the 5-scenario merge.

## Layout

```
skill-evaluator/
├── SKILL.md            # orchestration: 8-step flow, indexes only
├── reference.md        # details: metric map, sandbox, versioning, contracts, fallbacks
├── judges/             # LLM-judge rubrics (structured JSON output, temp=0)
├── scripts/            # deterministic tooling (each script = one documented CLI seam)
└── tests/              # 104 tests, subprocess-seam based
docs/adr/               # 6 architecture decisions
```

The evaluator is **self-contained**: all scripts and tests live inside the skill package (ADR-0006). Evaluation artifacts (eval sets, reports) live in `skill-evaluator/evalsets/<skill-name>/` and are frozen per version — never regenerated on re-runs.

## Usage

1. Install: copy `skill-evaluator/` into `~/.pi/agent/skills/`
2. Say: *"evaluate this skill: <path>"*
3. Get `report.json` + `report.md` under `skill-evaluator/evalsets/<name>/results/<version>/`

Optional: set `SKILL_EVAL_MODEL=<provider/id>` to pin the model used for sandbox runs and LLM judging (defaults to the pi default model).

## Key contracts

- **Report**: all 19 keys always present; unmeasured metrics are `skipped` (never scored 0); no weighted total — three-tier conclusion only (`all-pass` / `with-warnings` / `has-failures`).
- **Eval sets**: human-provided preferred; auto-generated ones require human review before freezing; frozen sets are reused across versions (that's what makes #14 evolution comparable).
- **Versioning**: git commit short hash; bare folders get `auto-<hash>` after an evaluator-side snapshot commit.
- **LLM judge outputs**: must pass `judge_runner.py --validate` before entering a report.

## Development

```bash
cd skill-evaluator
python -m pytest tests        # 104 tests, all green
```

Design decisions and their reasoning live in `docs/adr/`. See `README_CN.md` for the Chinese version.
