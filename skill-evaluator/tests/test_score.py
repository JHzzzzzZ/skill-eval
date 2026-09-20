"""Seam: python scripts/score.py <results目录> -> stdout JSON

输入文件（由 trace_run / 手工放置）：
- triggers.json: {"should": [true,false,...], "should_not": [...], "confusable": [...]}
  布尔值 = 该 prompt 是否实际触发了 skill
- runs.json: [{"tool_calls": int, "tokens": int, "seconds": float}, ...]

stdout 契约：
{
  "trigger": {"precision", "recall", "f1"},
  "cost": {"tool_calls": {"mean","std","n","min","max"}, "tokens": {...}, "seconds": {...}},
  "passed": bool
}
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "score.py"


def run_score(workdir: Path, triggers: dict, runs: list) -> dict:
    (workdir / "triggers.json").write_text(json.dumps(triggers), encoding="utf-8")
    (workdir / "runs.json").write_text(json.dumps(runs), encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(workdir)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"script failed: {r.stderr}"
    return json.loads(r.stdout)


# --- Slice 4: P/R/F1 ---

def test_prf1_all_correct(tmp_path):
    out = run_score(tmp_path,
        {"should": [True, True], "should_not": [False, False], "confusable": [False]},
        [])
    assert out["trigger"]["precision"] == 1.0
    assert out["trigger"]["recall"] == 1.0
    assert out["trigger"]["f1"] == 1.0


def test_prf1_half_recall(tmp_path):
    # 2 条应触发只触发 1 条 → recall 0.5；不应触发全没触发 → precision 1.0
    out = run_score(tmp_path,
        {"should": [True, False], "should_not": [False], "confusable": []},
        [])
    assert out["trigger"]["recall"] == 0.5
    assert out["trigger"]["precision"] == 1.0
    # F1 = 2PR/(P+R) = 2/3
    assert abs(out["trigger"]["f1"] - 2 / 3) < 1e-9


def test_prf1_no_positive_cases(tmp_path):
    # 全部不触发、也无应触发 → 无定义，score 应标 skipped 而非除零崩溃
    out = run_score(tmp_path,
        {"should": [], "should_not": [False], "confusable": [False]},
        [])
    assert out["trigger"] == "skipped"


# --- Slice 4b: 失败路径（P1-1/P2-5/6）---

def test_missing_input_files_yields_skipped(tmp_path):
    # 评测集未冻结是正常路径：triggers.json 不存在必须 skipped 而非崩溃
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["trigger"] == "skipped"
    assert out["cost"] == {"tool_calls": "skipped", "tokens": "skipped", "seconds": "skipped"}


def test_malformed_input_yields_skipped(tmp_path):
    (tmp_path / "triggers.json").write_text("not json", encoding="utf-8")
    (tmp_path / "runs.json").write_text("[]", encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["trigger"] == "skipped"


# --- Slice 5b: 必要性 A/B（spec P1: #5 数据通路）---

def test_necessity_with_baseline(tmp_path):
    # golden：有 skill；baseline：无 skill。skill 使 token 更低 → 必要性成立
    (tmp_path / "runs.json").write_text(
        json.dumps([{"tool_calls": 3, "tokens": 100, "seconds": 10.0}]), encoding="utf-8")
    (tmp_path / "baseline.json").write_text(
        json.dumps([{"tool_calls": 3, "tokens": 300, "seconds": 10.0}]), encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    n = out["necessity"]
    # #11 三分量：tokens / tool_calls / seconds 各自对比
    assert n["tokens"]["golden"] == 100.0
    assert n["tokens"]["baseline"] == 300.0
    # 提升率 = (baseline - golden) / baseline = 2/3
    assert abs(n["tokens"]["improvement"] - 2 / 3) < 1e-9
    assert n["tool_calls"]["golden"] == 3.0
    assert n["seconds"]["golden"] == 10.0


def test_necessity_without_baseline_is_skipped(tmp_path):
    (tmp_path / "runs.json").write_text(json.dumps([]), encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["necessity"] == "skipped"


# --- Slice 5: 成本统计 均值/方差 ---

def test_cost_mean_variance(tmp_path):
    runs = [
        {"tool_calls": 2, "tokens": 100, "seconds": 10.0},
        {"tool_calls": 4, "tokens": 200, "seconds": 20.0},
    ]
    out = run_score(tmp_path, {"should": [], "should_not": [], "confusable": []}, runs)
    c = out["cost"]["tool_calls"]
    assert c["mean"] == 3.0
    # #10/#28: 标准差用总体 std = sqrt(总体方差) = sqrt(1) = 1；并给出 n/min/max
    assert c["std"] == 1.0
    assert c["n"] == 2
    assert c["min"] == 2
    assert c["max"] == 4
    assert out["cost"]["tokens"]["mean"] == 150.0
    assert out["cost"]["seconds"]["mean"] == 15.0


def test_cost_empty_runs(tmp_path):
    out = run_score(tmp_path, {"should": [], "should_not": [], "confusable": []}, [])
    # 无 runs 时 cost 各键各自标 skipped（比整块 skipped 信息量更大）
    assert out["cost"] == {"tool_calls": "skipped", "tokens": "skipped", "seconds": "skipped"}


# --- 全仓复审 P2: 类型守卫 + n ---

def test_wrong_types_yield_skipped(tmp_path):
    # runs.json 顶层是数字数组、triggers.should 是字符串 → skipped 而非崩溃/垃圾 P/R
    (tmp_path / "triggers.json").write_text(json.dumps({"should": "abc"}), encoding="utf-8")
    (tmp_path / "runs.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    r = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["trigger"] == "skipped"
    assert out["cost"] == {"tool_calls": "skipped", "tokens": "skipped", "seconds": "skipped"}


def test_cost_stats_include_n(tmp_path):
    runs = [{"tool_calls": 2, "tokens": 100, "seconds": 10.0},
            {"tool_calls": 4, "tokens": 200, "seconds": 20.0}]
    out = run_score(tmp_path, {"should": [], "should_not": [], "confusable": []}, runs)
    assert out["cost"]["tool_calls"]["n"] == 2
    assert out["cost"]["tokens"]["min"] == 100
    assert out["cost"]["tokens"]["max"] == 200
