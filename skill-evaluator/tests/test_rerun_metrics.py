"""单指标重跑：只替换目标 rubric，其他裁决/文件原样保留。"""
import hashlib
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import rerun_metrics  # noqa: E402


def make_results(tmp_path: Path) -> Path:
    results = tmp_path / "results"
    jdir = results / "judges"
    jdir.mkdir(parents=True)
    for i, metric in enumerate(rerun_metrics.RUBRICS):
        (jdir / f"{metric}.json").write_text(json.dumps({
            "items": [{"name": metric, "pass": True, "quote": "x"}],
            "score": 1.0, "evidence": [], "reason": ""}), encoding="utf-8")
    (results / "run-meta.json").write_text(json.dumps({
        "skill": str(tmp_path / "skill"), "tier": "t1", "model": "m",
        "harness": "pi", "evalset": None}), encoding="utf-8")
    (results / "report.json").write_text("{}", encoding="utf-8")
    (tmp_path / "skill").mkdir()
    (tmp_path / "skill" / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n",
                                                  encoding="utf-8")
    return results


def hashes(jdir: Path) -> dict:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in jdir.glob("*.json")}


def test_only_target_rubric_changes_and_report_rebuilt(tmp_path):
    results = make_results(tmp_path)
    jdir = results / "judges"
    before = hashes(jdir)
    calls = []

    def judge_fn(cmd):
        calls.append(cmd)
        out = Path(cmd[cmd.index("--out") + 1])
        out.write_text(json.dumps({"items": [], "score": 0.75, "evidence": [],
                                   "reason": "single"}), encoding="utf-8")

    report_calls = []
    out = rerun_metrics.rerun_metric(results, "#2", judge_fn=judge_fn,
                                     report_fn=lambda cmd: report_calls.append(cmd))
    after = hashes(jdir)
    assert out["ok"] and out["rubric"] == "brevity"
    assert len(calls) == 1 and report_calls
    assert calls[0][calls[0].index("--metric") + 1] == "brevity"
    assert before["brevity.json"] != after["brevity.json"]
    assert {k: v for k, v in after.items() if k != "brevity.json"} == {
        k: v for k, v in before.items() if k != "brevity.json"}
    assert (results / "rerun-metrics.jsonl").is_file()


def test_judge_failure_leaves_old_rubric_untouched(tmp_path):
    results = make_results(tmp_path)
    before = hashes(results / "judges")

    def fail(cmd):
        raise RuntimeError("model down")

    with pytest.raises(RuntimeError, match="model down"):
        rerun_metrics.rerun_metric(results, "#5", judge_fn=fail,
                                   report_fn=lambda cmd: None)
    assert hashes(results / "judges") == before


def test_requires_all_t1_judges(tmp_path):
    results = make_results(tmp_path)
    (results / "judges" / "pii.json").unlink()
    with pytest.raises(ValueError, match="不完整"):
        rerun_metrics.rerun_metric(results, "#2", judge_fn=lambda cmd: None,
                                   report_fn=lambda cmd: None)


def test_normalize_metric_and_unknown():
    assert rerun_metrics.normalize_metric("2") == "#2"
    assert rerun_metrics.normalize_metric("#9") == "#9"
    with pytest.raises(ValueError):
        rerun_metrics.normalize_metric("#13")


def test_contract_serves_both_8_and_9():
    assert rerun_metrics.normalize_metric("#8") == "#8"
    assert rerun_metrics.normalize_metric("#9") == "#9"
    assert rerun_metrics.METRIC_RUBRIC["#8"] == rerun_metrics.METRIC_RUBRIC["#9"] == "contract"
