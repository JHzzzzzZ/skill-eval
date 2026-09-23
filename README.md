# skill-evaluator

A skill that evaluates other skills — against 19 quality metrics, producing a machine-readable `report.json` and a human-readable `report.md`.

It is itself a skill (for [pi](https://github.com/earendil-works/pi-coding-agent)), not a web service: copy it into your skills directory, point it at any skill package, and run it.

## What it checks

19 metrics are grouped by **measurement method**, not run one-by-one:

| Method | Metrics | Tooling |
|---|---|---|
| Static analysis | #2 name/description rules & token budget, #7 invocation mode, #13 dangerous-command scan, #3 body line count (primary source) | `scripts/static_check.py` |
| Sandbox runs | #1 trigger precision/recall/F1, #4 cost stats, #5 necessity A/B, #8 minimal deps, #9 result verification, #10 process audit, #12 stability, #15 idempotency | `scripts/trace_run.py`, `score.py`, `trigger_judge.py`, `idem.py`, `compare.py`, `process.py` |
| LLM review | #3 brevity (two semantic checkpoints, supporting evidence), #6 redundancy, #11 fallback, #16 pre-check, #17/18 I/O contract, #19 side-effect reversibility | `judges/*.md` rubrics + `judge_runner.py` |
| Historical diff | #14 version evolution | `scripts/evolution.py` |
| Trustworthiness | #1 eval-set quality (description echo / duplicates / cross-group conflicts), #12 cross-model agreement | `scripts/evalset_check.py`, `model_robust.py` |

Several metrics share one sandbox run — the 6 scenarios are grouped into incremental tiers (T0–T4, default T2); see `skill-evaluator/reference.md` (§ Run Scenarios) and ADR-0014.

## Layout

```
skill-evaluator/
├── SKILL.md            # orchestration: tiered flow (T0–T4, default T2), indexes only
├── reference.md        # details: tier definitions, metric map, sandbox, versioning, contracts, fallbacks
├── LICENSE             # MIT
├── requirements.txt    # zero runtime deps (stdlib); pytest for tests only
├── judges/             # LLM-judge rubrics (structured JSON output, fixed single-shot prompt)
├── scripts/            # deterministic tooling; check-deps.sh = preflight (#16)
└── tests/              # 250 tests, subprocess-seam based
docs/adr/               # 22 architecture decisions
```

The evaluator is **self-contained**: all scripts and tests live inside the skill package (ADR-0006). Evaluation artifacts (eval sets, reports) live in `skill-evaluator/evalsets/<skill-name>/` and are frozen per version — never regenerated on re-runs.

## Usage

1. Install: copy `skill-evaluator/` into `~/.pi/agent/skills/`
2. Preflight: `bash scripts/check-deps.sh` (python >= 3.9, git, pytest, pi CLI)
3. Say: *"evaluate this skill: <path>"*
4. Get `report.json` + `report.md` + `meta.json` under `skill-evaluator/evalsets/<name>/results/<version>/` — pass `--skill <path>` to have `meta.json` record the skill's own content hash (ADR-0010)

Optional:

- `SKILL_EVAL_MODEL=<provider/id>`: pin the model used for sandbox runs and LLM judging (defaults to the pi default model).
- `SKILL_EVAL_REVIEW_MODEL=<provider/id>`: model for AI review of eval sets (must differ from the generation model; if unset, picked interactively on first run).

Tier entry: `--tier <static|review|trigger|core|full>` (default T2); higher tiers reuse lower-tier artifacts (validated against the evaluated skill's content hash).

## Key contracts

- **Report**: all 19 keys always present; unmeasured metrics are `skipped` (never scored 0); no weighted total — three-tier conclusion only (`all-pass` / `with-warnings` / `has-failures`); a `tier` field records the highest tier reached.
- **Eval sets**: human-provided preferred; auto-generated ones are frozen after AI review with a different model (`reviewed_by: "ai"`), upgradable to `"human"` after manual re-review; frozen sets are reused across versions (that's what makes #14 evolution comparable).
- **Versioning**: git commit short hash; bare folders get `auto-<hash>` after an evaluator-side snapshot commit.
- **LLM judge outputs**: must pass `judge_runner.py --validate` before entering a report.
- **Evaluator fingerprint**: `report.json` and `results/meta.json` record the evaluator's own content hash; `evolution.py` flags `comparable=false` when two versions came from different evaluators, instead of reading the diff as a regression (ADR-0010).

## Development

```bash
cd skill-evaluator
python -m pytest tests        # 250 tests, all green
```

Design decisions and their reasoning live in `docs/adr/`. See `README_CN.md` for the Chinese version.
