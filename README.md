# skill-evaluator

A skill that evaluates other skills — against 16 quality metrics, producing a machine-readable `report.json` and a human-readable `report.md`.

It is itself a skill (for [pi](https://github.com/earendil-works/pi-coding-agent)), not a web service: copy it into your skills directory, point it at any skill package, and run it.

## What it checks

16 metrics are grouped by **measurement method**, not run one-by-one:

| Method | Metrics | Tooling |
|---|---|---|
| Static analysis | #1 name/description rules & character budget (384 warn / 1024 fail), #3 invocation mode, #4 dangerous-command + egress scan, #2 body line count (150 warn / 500 fail), #12 dependency completeness | `scripts/static_check.py` |
| Sandbox runs | #10 trigger P/R/F1, #13 cost, #11 necessity A/B, #12 minimal deps, #14 result verification, #15 stability, #16 idempotency | `scripts/trace_run.py`, `score.py`, `trigger_judge.py`, `idem.py`, `compare.py` |
| LLM review | #2 brevity (semantic), #5 redundancy, #6 fallback, #7 pre-check, #8/#9 I/O contract, #4 PII semantics (employee IDs / customer names / internal hosts) | `judges/*.md` rubrics + `judge_runner.py` |
| Trustworthiness | #10 eval-set quality (description echo / duplicates / cross-group conflicts), #15 cross-model agreement | `scripts/evalset_check.py`, `model_robust.py` |

Several metrics share one sandbox run — the 6 scenarios are grouped into incremental tiers (T0 / T1 / T2A / T2B / T3, default T2A); see `skill-evaluator/reference.md` (§ Run Scenarios) and ADR-0033.

## Layout

```
skill-evaluator/
├── SKILL.md            # orchestration: tiered flow (T0 / T1 / T2A / T2B / T3, default T2A), indexes only
├── reference.md        # details: tier definitions, metric map, sandbox, versioning, contracts, fallbacks
├── config.yaml         # tunable thresholds (384/1024 chars, 150/500 lines, ...); part of the fingerprint
├── LICENSE             # MIT
├── requirements.txt    # zero runtime deps (stdlib); pytest for tests only
├── judges/             # LLM-judge rubrics (structured JSON output, fixed single-shot prompt)
├── scripts/            # CLI + runner + llm/harness adapters; check-deps.sh = preflight (#7)
├── web/                # local UI (stdlib HTTP + SSE + vanilla JS)
└── tests/              # 316 tests, subprocess-seam based
docs/adr/               # 38 architecture decisions
```

The evaluator is **self-contained**: all scripts and tests live inside the skill package (ADR-0006). Evaluation artifacts (eval sets, reports) live in `skill-evaluator/evalsets/<skill-name>/` and are frozen per version — never regenerated on re-runs.

## Usage

1. Install: copy `skill-evaluator/` into `~/.pi/agent/skills/`
2. Preflight: `bash scripts/check-deps.sh` (python >= 3.9, git, pytest, pi CLI, config.yaml parses)
3. Say: *"evaluate this skill: <path>"* — or open the local UI and upload a `.zip` (external users).
4. Or run the deterministic pipeline / local UI:
   ```bash
   python scripts/runner.py --skill <path> --tier t2a
   python web/app.py    # http://127.0.0.1:8765
   ```
5. Get `report.json` + `report.md` + `report.html` + `meta.json` under the results directory.
6. In the UI: check/generate/review an eval set, test the LLM connection, and manually downgrade a `fail` to `warn` with an audit reason.

Optional:

- `SKILL_EVAL_MODEL=<provider/id>`: pin the model used for T2+ sandbox runs (defaults to the pi default model).
- T1 direct LLM: UI fields > `config.yaml` (`llm.base_url` / `llm.model` / `llm.api_key_env`) > standard env (`OPENAI_API_KEY` / `ANTHROPIC_API_KEY`, `OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL`, `SKILL_EVAL_MODEL`) > official endpoint. The UI key is used for this run only and is never written to disk. `--backend pi` falls back to the old path.
- `SKILL_EVAL_REVIEW_MODEL=<provider/id>`: model for AI review of eval sets (must differ from the generation model; if unset, picked interactively on first run).

Tier entry: `--tier <t0|t1|t2a|t2b|t3>` (default T2A); higher tiers reuse lower-tier artifacts (validated against the evaluated skill's content hash).

## Key contracts

- **Report**: all 16 keys always present; unmeasured metrics are `skipped` (never scored 0); no weighted total — three-tier conclusion only (`all-pass` / `with-warnings` / `has-failures`); a `tier` field records the highest tier reached.
- **Eval sets**: human-provided preferred; auto-generated ones are frozen after AI review with a different model (`reviewed_by: "ai"`), upgradable to `"human"` after manual re-review; frozen sets are reused across versions (that's what makes cross-version comparison possible).
- **Versioning**: git commit short hash; bare folders get `auto-<hash>` after an evaluator-side snapshot commit.
- **LLM judge outputs**: must pass `judge_runner.py --validate` before entering a report; T1 calls the `llm` layer directly (no tools), T2+ uses the `harness` adapter.
- **Evaluator fingerprint**: `report.json` (schema 2) and `results/meta.json` record the evaluator's own content hash; different fingerprints or schemas mean cross-version diffs are not regression evidence (ADR-0010/0032).

## Development

```bash
cd skill-evaluator
python -m pytest tests        # 316 tests, all green
```

Design decisions and their reasoning live in `docs/adr/`. See `README_CN.md` for the Chinese version.
