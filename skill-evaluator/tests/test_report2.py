"""report.py 断链修复（process/ablation 通路 + #7/#12/路径守卫）"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "report.py"


def run_report(d: Path):
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(d)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, f"report.py failed: {r.stderr}"
    return json.loads((d / "report.json").read_text(encoding="utf-8"))


# --- Slice 27: report.py 断链修复（process/ablation 通路 + #7/#12/路径守卫）---

def write_inputs(d: Path, files: dict):
    for name, data in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_process_verdict_reaches_report(tmp_path):
    # 全仓复审 P1：#10 硬编码 skipped，process.json 无通路
    write_inputs(tmp_path, {"process.json": {"meaningless": [], "score": 2, "reason": "一致"}})
    out = run_report(tmp_path)
    assert out["metrics"]["#10"]["verdict"] == "pass"
    assert out["metrics"]["#10"]["data"]["score"] == 2


def test_ablation_verdict_reaches_report(tmp_path):
    # #6 实证：消融后 F1 下降 → skill 正文必要；上升/持平 → 冗余实证
    write_inputs(tmp_path, {"ablation.json": {
        "f1_full": 1.0, "f1_ablated": 0.5, "deleted": ["概念说明"]}})
    out = run_report(tmp_path)
    assert out["metrics"]["#6"]["data"]["f1_ablated"] == 0.5
    assert out["metrics"]["#6"]["verdict"] == "pass"  # 消融后掉分 = 原文必要


def test_static_seven_malformed_bool_is_warn(tmp_path):
    # #7 新契约：disable-model-invocation 非布尔 → warning → #7 warn，不影响 #2/#13
    write_inputs(tmp_path, {"static.json": {
        "name": "s", "description": "d", "description_tokens": 50,
        "invoke": {"resolved": "both"},
        "errors": [], "warnings": ["disable-model-invocation 取值 'maybe' 不是布尔（pi 将按未知字段忽略）（#7）"],
        "dangerous": [], "passed": True}})
    out = run_report(tmp_path)
    assert out["metrics"]["#7"]["verdict"] == "warn"
    assert out["metrics"]["#2"]["verdict"] == "pass"  # #7 的警告不污染 #2
    assert out["metrics"]["#13"]["verdict"] == "pass"


def test_idempotency_needs_multiple_runs(tmp_path):
    # P2: 单次运行不判 #12/#15
    write_inputs(tmp_path, {"score.json": {
        "trigger": "skipped",
        "cost": {"tool_calls": {"mean": 3, "std": 0, "n": 1}, "tokens": {"mean": 100, "std": 0, "n": 1},
                 "seconds": {"mean": 10, "std": 0, "n": 1}},
        "necessity": "skipped", "passed": True}})
    out = run_report(tmp_path)
    assert out["metrics"]["#12"]["verdict"] == "warn"  # n=1 → warn "单次运行"
    assert "仅单次运行" in out["metrics"]["#12"]["note"]


def test_results_dir_missing_no_crash(tmp_path):
    # P2: 写路径也要兜底——目录不存在时受控失败
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "nonexistent")],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode != 0
    assert "traceback" not in r.stderr.lower()


# --- Slice 28: #14 归组修正——method 应为「历史对比」，不是「沙箱运行」 ---

def test_evolution_method_is_historical_diff(tmp_path):
    # #14 = evolution.py 读历史 report.json，与沙箱无关；ADR-0004 四分组归「历史对比」
    out = run_report(tmp_path)
    assert out["metrics"]["#14"]["method"] == "历史对比"
    assert out["metrics"]["#14"]["verdict"] == "skipped"  # evolution.py 独立产出，report.py 不代打


# --- reviewed_by 路径修复：evalset 版本目录（v1）≠ results 版本目录（auto-*） ---

def test_reviewed_by_found_via_v_glob(tmp_path):
    # 回归：旧实现拼 d.parent.parent/d.name → evalsets/<name>/<结果版本号>/meta.json（不存在）
    name = "sample-skill"
    (tmp_path / name / "v1").mkdir(parents=True)
    (tmp_path / name / "v1" / "meta.json").write_text(
        json.dumps({"frozen_at": "t", "reviewed_by": "ai"}), encoding="utf-8")
    res = tmp_path / name / "results" / "auto-b9d7fab"
    res.mkdir(parents=True)
    out = run_report(res)
    assert out["reviewed_by"] == "ai"
    assert "AI 审核" in out["metrics"]["#1"]["note"] or out["metrics"]["#1"]["verdict"] == "skipped"


def test_reviewed_by_picks_highest_version(tmp_path):
    name = "sample-skill"
    for v, by in (("v2", "ai"), ("v10", "human")):
        (tmp_path / name / v).mkdir(parents=True)
        (tmp_path / name / v / "meta.json").write_text(
            json.dumps({"reviewed_by": by}), encoding="utf-8")
    res = tmp_path / name / "results" / "auto-x"
    res.mkdir(parents=True)
    out = run_report(res)
    assert out["reviewed_by"] == "human"  # v10 > v2，字典序陷阱
