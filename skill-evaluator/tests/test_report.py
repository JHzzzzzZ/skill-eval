"""Seam: python scripts/report.py <results目录> [--out <前缀>] -> stdout JSON 摘要 + 落盘 report.json/report.md

输入（同目录，均可缺省→对应指标 skipped）：
- static.json（static_check.py stdout）
- score.json（score.py stdout）
- idem.json（{"ratio", "idempotent"}，多次 idem.py 的聚合）
- compare.json（compare.py 输出，#9）
- golden.json（golden trace，#8）
- judges/<metric>.json（judge_runner 校验过的输出）：brevity(#3) redundancy(#6) fallback(#11)
  precheck(#16) contract(#17,#18) side-effects(#19)

report.json 契约（ADR-0004）：19 个 key 全出现；值 = {"verdict": pass|warn|fail|skipped,
"method": 静态/运行/LLM评审/对比, "data": {...}, "note"}；不合成总分；
conclusion 三档：all-pass / with-warnings / has-failures（全 skipped 也算 with-warnings）。
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "report.py"

ALL_KEYS = ["#1", "#2", "#3", "#4", "#5", "#6", "#7", "#8", "#9", "#10",
            "#11", "#12", "#13", "#14", "#15", "#16", "#17", "#18", "#19"]


def write_inputs(d: Path, files: dict):
    for name, data in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def run_report(d: Path):
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(d)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"report.py failed: {r.stderr}"
    return json.loads((d / "report.json").read_text(encoding="utf-8"))


# --- Slice 15: 19 key 恒定 + skipped 语义 ---

def test_empty_inputs_all_skipped(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    out = run_report(tmp_path)
    assert len(out["metrics"]) == 19
    for k, v in out["metrics"].items():
        assert v["verdict"] == "skipped", f"{k} 应 skipped"
    assert out["conclusion"] == "with-warnings"  # 全 skipped 不是 all-pass


# --- Slice 16: 静态检查映射（#2/#7/#13）---

def test_static_inputs(tmp_path):
    write_inputs(tmp_path, {"static.json": {
        "name": "s", "description": "d", "description_tokens": 50,
        "invoke": {"resolved": "both"},
        "errors": [], "warnings": [], "dangerous": [], "passed": True}})
    out = run_report(tmp_path)
    m = out["metrics"]
    assert m["#2"]["verdict"] == "pass"
    assert m["#7"]["verdict"] == "pass"
    assert m["#13"]["verdict"] == "pass"
    assert out["conclusion"] == "with-warnings"  # 其余仍 skipped


def test_static_dangerous_is_fail(tmp_path):
    write_inputs(tmp_path, {"static.json": {
        "name": "s", "description": "d", "description_tokens": 50,
        "invoke": {"resolved": "both"},
        "errors": ["发现 1 处危险命令模式"], "warnings": [],
        "dangerous": [{"pattern": "x"}], "passed": False}})
    out = run_report(tmp_path)
    assert out["metrics"]["#13"]["verdict"] == "fail"
    assert out["conclusion"] == "has-failures"


# --- Slice 17: 触发/成本/必要性/幂等/对比 ---

def test_score_inputs(tmp_path):
    write_inputs(tmp_path, {"score.json": {
        "trigger": {"precision": 1.0, "recall": 1.0, "f1": 1.0},
        "cost": {"tool_calls": {"mean": 3, "var": 1}, "tokens": {"mean": 100, "var": 0},
                 "seconds": {"mean": 10, "var": 0}},
        "necessity": {"golden_tokens_mean": 100, "baseline_tokens_mean": 300,
                      "improvement": 0.66},
        "passed": True}})
    out = run_report(tmp_path)
    m = out["metrics"]
    assert m["#1"]["verdict"] == "pass"   # f1 >= 0.7
    assert m["#4"]["verdict"] == "pass"   # 有成本数据即 info-pass
    assert m["#5"]["verdict"] == "pass"   # improvement > 0


def test_low_f1_is_fail(tmp_path):
    write_inputs(tmp_path, {"score.json": {
        "trigger": {"precision": 0.5, "recall": 0.5, "f1": 0.5},
        "cost": "skipped", "necessity": "skipped", "passed": True}})
    out = run_report(tmp_path)
    assert out["metrics"]["#1"]["verdict"] == "fail"


def test_idem_and_compare(tmp_path):
    write_inputs(tmp_path, {
        "idem.json": {"ratio": 0.2, "idempotent": True},
        "compare.json": {"match": True, "score": 1, "reason": "ok"}})
    out = run_report(tmp_path)
    assert out["metrics"]["#15"]["verdict"] == "pass"
    assert out["metrics"]["#9"]["verdict"] == "pass"


# --- Slice 18: LLM 评审映射 ---

def test_judge_inputs(tmp_path):
    write_inputs(tmp_path, {"judges/redundancy.json": {"score": 2, "evidence": [], "reason": "x"},
                            "judges/fallback.json": {"score": 0, "evidence": ["a"], "reason": "无 fallback"}})
    out = run_report(tmp_path)
    m = out["metrics"]
    assert m["#6"]["verdict"] == "pass"   # score 2
    assert m["#11"]["verdict"] == "fail"  # score 0


# --- Slice 19: report.md 产出 ---

def test_report_md_written(tmp_path):
    write_inputs(tmp_path, {"static.json": {
        "name": "s", "description": "d", "description_tokens": 50,
        "invoke": {"resolved": "both"}, "errors": [], "warnings": [],
        "dangerous": [], "passed": True}})
    run_report(tmp_path)
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "#1" in md and "结论" in md and "pass" in md


# --- 全仓复审 P2: #12 稳定性阈值（变异系数）---

def test_idempotency_high_variance_warns(tmp_path):
    write_inputs(tmp_path, {"score.json": {
        "trigger": "skipped",
        "cost": {"tool_calls": {"mean": 10, "var": 50, "n": 3}, "tokens": {"mean": 100, "var": 400, "n": 3},
                 "seconds": {"mean": 10, "var": 0, "n": 3}},
        "necessity": "skipped", "passed": True}})
    out = run_report(tmp_path)
    # 变异系数 sqrt(50)/10 ≈ 0.707 > 0.5 → 不稳定
    assert out["metrics"]["#12"]["verdict"] == "warn"
