"""report.py 断链修复（process/ablation 通路 + #7/#12/路径守卫）"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "report.py"


def run_report(d: Path):
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(d)],
        capture_output=True, text=True,
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


def test_static_seven_reads_errors_not_resolved(tmp_path):
    # P1: static_check 对非法 invoke 记 error 但 resolved 保持默认 both → #7 必须看 errors
    write_inputs(tmp_path, {"static.json": {
        "name": "s", "description": "d", "description_tokens": 50,
        "invoke": {"resolved": "both"},
        "errors": ["invoke 取值 'whatever' 不在 [...]（#7）"], "warnings": [],
        "dangerous": [], "passed": False}})
    out = run_report(tmp_path)
    assert out["metrics"]["#7"]["verdict"] == "fail"
    assert out["metrics"]["#2"]["verdict"] == "pass"  # #7 的错误不污染 #2
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


def test_results_dir_missing_no_crash(tmp_path):
    # P2: 写路径也要兜底——目录不存在时受控失败
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "nonexistent")],
        capture_output=True, text=True)
    assert r.returncode != 0
    assert "traceback" not in r.stderr.lower()
