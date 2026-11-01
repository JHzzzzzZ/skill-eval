"""runner.py 的 dry-run / T0 端到端（不调用 pi / LLM）。"""
import json
import subprocess
import sys
from pathlib import Path
import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from runner import RunnerError, _real_cases  # noqa: E402

SCRIPT = Path(__file__).parent.parent / "scripts" / "runner.py"


def make_skill(tmp_path: Path) -> Path:
    d = tmp_path / "sample"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: sample\ndescription: d\n---\nbody\n", encoding="utf-8")
    return d


def test_dry_run_plan(tmp_path):
    skill = make_skill(tmp_path)
    r = subprocess.run([sys.executable, str(SCRIPT), "--skill", str(skill),
                        "--tier", "t2b", "--dry-run"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    plan = json.loads(r.stdout.splitlines()[0])
    assert plan["type"] == "plan"
    assert plan["stages"] == ["t0", "t1", "t2a", "t2b"]


def test_t0_end_to_end_writes_schema2(tmp_path):
    skill = make_skill(tmp_path)
    results = tmp_path / "results"
    r = subprocess.run([sys.executable, str(SCRIPT), "--skill", str(skill),
                        "--tier", "t0", "--results", str(results)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    report = json.loads((results / "report.json").read_text(encoding="utf-8"))
    assert report["schema"] == 2
    assert len(report["metrics"]) == 16


def test_progress_has_clock_and_elapsed(tmp_path):
    skill = make_skill(tmp_path)
    results = tmp_path / "results"
    progress = tmp_path / "progress.ndjson"
    r = subprocess.run([sys.executable, str(SCRIPT), "--skill", str(skill),
                        "--tier", "t0", "--results", str(results),
                        "--progress", str(progress)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    events = [json.loads(line) for line in progress.read_text(encoding="utf-8").splitlines()]
    start = next(e for e in events if e["type"] == "stage" and e.get("status") == "start")
    done = next(e for e in events if e["type"] == "stage" and e.get("status") == "done")
    assert start["clock"]
    assert done["elapsed_s"] >= 0
    overall = next(e for e in events if e["type"] == "done")
    assert overall["elapsed_s"] >= done["elapsed_s"]


def test_t0_reuses_artifacts_and_can_force_rerun(tmp_path):
    skill = make_skill(tmp_path)
    results = tmp_path / "results"
    first = tmp_path / "first.ndjson"
    second = tmp_path / "second.ndjson"
    third = tmp_path / "third.ndjson"
    base = [sys.executable, str(SCRIPT), "--skill", str(skill), "--tier", "t0",
            "--results", str(results)]
    for prog in (first, second, third):
        extra = ["--rerun-t0"] if prog == third else []
        r = subprocess.run(base + ["--progress", str(prog)] + extra,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert r.returncode == 0, r.stderr
    def t0(prog):
        events = [json.loads(x) for x in prog.read_text(encoding="utf-8").splitlines()]
        return next(e for e in events if e["type"] == "stage" and e.get("name") == "T0"
                    and e.get("status") == "done")
    assert t0(second)["cached"] is True
    assert t0(third)["cached"] is False


def test_t0_cache_crosses_results_dirs(tmp_path):
    skill = make_skill(tmp_path)
    (skill / "SKILL.md").write_text(
        f"---\nname: sample-{tmp_path.name}\ndescription: d\n---\nbody\n", encoding="utf-8")
    first = tmp_path / "first"
    second = tmp_path / "second"
    p1 = tmp_path / "p1.ndjson"
    p2 = tmp_path / "p2.ndjson"
    base = [sys.executable, str(SCRIPT), "--skill", str(skill), "--tier", "t0"]
    r = subprocess.run(base + ["--results", str(first), "--progress", str(p1)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    r = subprocess.run(base + ["--results", str(second), "--progress", str(p2)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    events = [json.loads(x) for x in p2.read_text(encoding="utf-8").splitlines()]
    done = next(e for e in events if e["type"] == "stage" and e.get("name") == "T0"
                and e.get("status") == "done")
    assert done["cached"] is True


def test_t1_cache_crosses_results_dirs(tmp_path):
    skill = make_skill(tmp_path)
    (skill / "SKILL.md").write_text(
        f"---\nname: cache-{tmp_path.name}\ndescription: d\n---\nbody\n", encoding="utf-8")
    base = [sys.executable, str(SCRIPT), "--skill", str(skill), "--backend", "pi"]
    first = tmp_path / "first"
    second = tmp_path / "second"
    r = subprocess.run(base + ["--tier", "t0", "--results", str(first)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    jdir = first / "judges"
    jdir.mkdir()
    for metric in ("brevity", "redundancy", "fallback", "precheck", "contract", "deps", "pii"):
        (jdir / f"{metric}.json").write_text(json.dumps({
            "items": [{"name": "x", "pass": True, "quote": "x"}],
            "score": 1.0, "evidence": [], "reason": ""}), encoding="utf-8")
    r = subprocess.run(base + ["--tier", "t1", "--results", str(first)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    p2 = tmp_path / "p2.ndjson"
    r = subprocess.run(base + ["--tier", "t1", "--results", str(second), "--progress", str(p2)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    events = [json.loads(x) for x in p2.read_text(encoding="utf-8").splitlines()]
    t1 = next(e for e in events if e["type"] == "stage" and e.get("name") == "T1"
              and e.get("status") == "done")
    assert t1["cached"] is True


def test_t1_reuses_valid_judges_without_llm(tmp_path):
    skill = make_skill(tmp_path)
    results = tmp_path / "results"
    first = tmp_path / "t0.ndjson"
    r = subprocess.run([sys.executable, str(SCRIPT), "--skill", str(skill), "--tier", "t0",
                        "--results", str(results), "--backend", "pi",
                        "--progress", str(first)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    jdir = results / "judges"
    jdir.mkdir()
    for metric in ("brevity", "redundancy", "fallback", "precheck", "contract", "deps", "pii"):
        (jdir / f"{metric}.json").write_text(json.dumps({
            "items": [{"name": "x", "pass": True, "quote": "x"}],
            "score": 1.0, "evidence": [], "reason": ""}), encoding="utf-8")
    second = tmp_path / "t1.ndjson"
    r = subprocess.run([sys.executable, str(SCRIPT), "--skill", str(skill), "--tier", "t1",
                        "--results", str(results), "--backend", "pi",
                        "--progress", str(second)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    events = [json.loads(x) for x in second.read_text(encoding="utf-8").splitlines()]
    t1 = next(e for e in events if e["type"] == "stage" and e.get("name") == "T1"
              and e.get("status") == "done")
    assert t1["cached"] is True


def test_missing_skill_is_error(tmp_path):
    r = subprocess.run([sys.executable, str(SCRIPT), "--skill", str(tmp_path),
                        "--tier", "t0", "--dry-run"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 2


def test_real_cases_jsonl_assigns_ids_and_optional_fields(tmp_path):
    f = tmp_path / "real.jsonl"
    f.write_text(
        '{"prompt":"p1","label":"legacy"}\n'
        '{"prompt":"p2","note":"备注","expect":"验收"}\n',
        encoding="utf-8")
    cases = _real_cases(f)
    assert [c["id"] for c in cases] == ["c001", "c002"]
    assert cases[0]["note"] == "legacy"
    assert cases[1]["expect"] == "验收"
    assert cases[1]["prompt"] == "p2"


def test_real_cases_prompt_is_required(tmp_path):
    f = tmp_path / "real.jsonl"
    f.write_text('{"note":"只有备注"}\n', encoding="utf-8")
    with pytest.raises(RunnerError, match="缺 prompt"):
        _real_cases(f)


def test_real_cases_limit_20(tmp_path):
    f = tmp_path / "real.jsonl"
    f.write_text("\n".join('{"prompt":"p"}' for _ in range(21)), encoding="utf-8")
    with pytest.raises(RunnerError, match="最多 20"):
        _real_cases(f)


# --- 阶段级检查点：只有完整阶段才写缓存 ---

RUBRICS = ("brevity", "redundancy", "fallback", "precheck", "contract", "deps", "pii")


def write_judges(d: Path, metrics=RUBRICS, score=1.0):
    d.mkdir(parents=True, exist_ok=True)
    for metric in metrics:
        (d / f"{metric}.json").write_text(json.dumps({
            "items": [{"name": "x", "pass": True, "quote": "x"}],
            "score": score, "evidence": [], "reason": ""}), encoding="utf-8")


def test_judges_ready_requires_all_seven(tmp_path):
    from runner import _judges_ready
    write_judges(tmp_path / "judges", RUBRICS[:3])
    assert _judges_ready(tmp_path / "judges") is False
    write_judges(tmp_path / "judges", RUBRICS[3:])
    assert _judges_ready(tmp_path / "judges") is True
    (tmp_path / "judges" / "pii.json").write_text('{"items": [], "score": 2}',
                                                   encoding="utf-8")
    assert _judges_ready(tmp_path / "judges") is False


def test_stage_ready_checks_required_fields(tmp_path):
    from runner import stage_ready
    assert stage_ready("t0", tmp_path) is False
    (tmp_path / "static.json").write_text("{}", encoding="utf-8")
    assert stage_ready("t0", tmp_path) is False
    (tmp_path / "static.json").write_text('{"passed": false}', encoding="utf-8")
    assert stage_ready("t0", tmp_path) is True

    (tmp_path / "triggers.json").write_text(
        '{"should": [true], "should_not": [], "confusable": []}', encoding="utf-8")
    assert stage_ready("t2a", tmp_path) is False
    (tmp_path / "score.json").write_text('{"trigger": {"f1": 1}}', encoding="utf-8")
    assert stage_ready("t2a", tmp_path) is True

    (tmp_path / "t2b").mkdir()
    (tmp_path / "t2b" / "score.json").write_text('{"necessity": "skipped"}', encoding="utf-8")
    (tmp_path / "t2b" / "runs.json").write_text("[]", encoding="utf-8")
    assert stage_ready("t2b", tmp_path) is False
    (tmp_path / "t2b" / "baseline.json").write_text("[]", encoding="utf-8")
    assert stage_ready("t2b", tmp_path) is True

    (tmp_path / "runs.json").write_text("[]", encoding="utf-8")
    assert stage_ready("t3", tmp_path) is False
    (tmp_path / "score.json").write_text(
        '{"trigger": "skipped", "cost": {}, "necessity": "skipped"}', encoding="utf-8")
    assert stage_ready("t3", tmp_path) is True


def test_t1_partial_judges_are_not_cached(tmp_path):
    skill = make_skill(tmp_path)
    (skill / "SKILL.md").write_text(
        f"---\nname: partial-{tmp_path.name}\ndescription: d\n---\nbody\n", encoding="utf-8")
    results = tmp_path / "results"
    write_judges(results / "judges", RUBRICS[:3])
    base = [sys.executable, str(SCRIPT), "--skill", str(skill), "--tier", "t1",
            "--results", str(results), "--backend", "llm",
            "--base-url", "http://127.0.0.1:9"]
    first = tmp_path / "first.ndjson"
    second = tmp_path / "second.ndjson"
    for prog in (first, second):
        r = subprocess.run(base + ["--progress", str(prog)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert r.returncode == 0, r.stderr
        events = [json.loads(x) for x in prog.read_text(encoding="utf-8").splitlines()]
        t1 = next(e for e in events if e["type"] == "stage" and e.get("name") == "T1"
                  and e.get("status") == "done")
        assert t1["cached"] is False
        assert t1["incomplete"] is True
        # 半截评审被整档丢弃，7 个 rubric 全部重跑
        assert sum(1 for e in events if e["type"] == "judge") == 7
