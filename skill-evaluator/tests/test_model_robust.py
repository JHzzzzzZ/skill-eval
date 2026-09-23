"""Seam: python scripts/model_robust.py <results目录> [--out f]

跨模型鲁棒性（ADR-0011）：同一批 case 换模型跑，triggered 与回答是否一致。
单模型 / 无 models/ 目录 → skipped=true（至少需要 2 个模型），不崩、不打 0 分。
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "model_robust.py"


def make_model(results: Path, model: str, traces: dict):
    d = results / "models" / model
    d.mkdir(parents=True, exist_ok=True)
    for idx, t in traces.items():
        (d / f"trace-{idx}.json").write_text(json.dumps(t, ensure_ascii=False), encoding="utf-8")


def run(results: Path):
    r = subprocess.run([sys.executable, str(SCRIPT), str(results)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


ANSWER = "结论：19 条指标里 3 条警告，建议先修 fallback 路径。"


# --- 一致 / 不一致 ---

def test_two_models_agree(tmp_path):
    traces = {1: {"triggered": True, "answer": ANSWER, "tool_calls": 4, "tokens": 900},
              2: {"triggered": True, "answer": ANSWER, "tool_calls": 5, "tokens": 1100}}
    make_model(tmp_path, "gpt-x", traces)
    make_model(tmp_path, "claude-y", traces)
    out = run(tmp_path)
    assert out["skipped"] is False
    assert out["models"] == ["claude-y", "gpt-x"]
    assert out["agreement_ratio"] == 1.0
    assert out["robust"] is True
    assert all(c["agree"] for c in out["case_detail"])


def test_answer_drift_not_robust(tmp_path):
    make_model(tmp_path, "a", {1: {"triggered": True, "answer": "结论是 6 条失败，先修权限", "tool_calls": 3}})
    make_model(tmp_path, "b", {1: {"triggered": True, "answer": "一切正常无问题，可以发布", "tool_calls": 3}})
    out = run(tmp_path)
    assert out["robust"] is False
    assert out["agreement_ratio"] == 0.0
    assert "漂移" in out["note"]


def test_trigger_disagreement_not_robust(tmp_path):
    make_model(tmp_path, "a", {1: {"triggered": True, "answer": ANSWER}})
    make_model(tmp_path, "b", {1: {"triggered": False, "answer": ANSWER}})
    out = run(tmp_path)
    assert out["robust"] is False
    assert out["case_detail"][0]["triggered_agree"] is False


def test_empty_answers_fall_back_to_trigger_signal(tmp_path):
    # 触发评测 early-exit 会截断回答：回答面无信息时只看 triggered
    make_model(tmp_path, "a", {1: {"triggered": True, "answer": ""}})
    make_model(tmp_path, "b", {1: {"triggered": True, "answer": ""}})
    out = run(tmp_path)
    assert out["case_detail"][0]["answer_similar"] is None
    assert out["comparable_cases"] == 1
    assert out["robust"] is True


def test_partial_answer_counts_as_disagreement(tmp_path):
    make_model(tmp_path, "a", {1: {"triggered": True, "answer": ANSWER}})
    make_model(tmp_path, "b", {1: {"triggered": True, "answer": ""}})
    out = run(tmp_path)
    assert out["case_detail"][0]["answer_similar"] is False
    assert out["robust"] is False


def test_partial_answer_with_early_exit_is_no_information(tmp_path):
    # early-exit 截断的空回答不算不一致（不是模型行为差异，是评测机制）
    make_model(tmp_path, "a", {1: {"triggered": True, "answer": ANSWER}})
    make_model(tmp_path, "b", {1: {"triggered": True, "answer": "", "early_exit": True}})
    out = run(tmp_path)
    assert out["case_detail"][0]["answer_similar"] is None
    assert out["robust"] is True


# --- 降级与守卫 ---

def test_single_model_skipped(tmp_path):
    make_model(tmp_path, "a", {1: {"triggered": True, "answer": ANSWER}})
    out = run(tmp_path)
    assert out["skipped"] is True
    assert out["robust"] is None
    assert "至少需要 2 个" in out["note"]


def test_no_models_dir_skipped(tmp_path):
    out = run(tmp_path)
    assert out["skipped"] is True
    assert out["agreement_ratio"] is None


def test_incomplete_cases_excluded(tmp_path):
    make_model(tmp_path, "a", {1: {"triggered": True, "answer": ANSWER},
                              2: {"triggered": True, "answer": ANSWER},
                              3: {"triggered": True, "answer": ANSWER}})
    make_model(tmp_path, "b", {1: {"triggered": True, "answer": ANSWER},
                              2: {"triggered": True, "answer": ANSWER}})
    out = run(tmp_path)
    assert out["incomplete_cases"] == [3]
    assert out["comparable_cases"] == 2
    assert out["agreement_ratio"] == 1.0


def test_broken_trace_recorded_not_crash(tmp_path):
    d = tmp_path / "models" / "a"
    d.mkdir(parents=True)
    (d / "trace-1.json").write_text("{坏的", encoding="utf-8")
    out = run(tmp_path)
    assert out["skipped"] is True
    assert any("无法解析" in e for e in out["errors"])


def test_cost_summary_per_model(tmp_path):
    make_model(tmp_path, "a", {1: {"triggered": True, "answer": ANSWER, "tokens": 100}})
    make_model(tmp_path, "b", {1: {"triggered": True, "answer": ANSWER, "tokens": 500}})
    out = run(tmp_path)
    assert out["cost"]["a"]["tokens_mean"] == 100
    assert out["cost"]["b"]["tokens_mean"] == 500
    assert "token 均值" in out["note"]


def test_missing_results_dir_exits_2(tmp_path):
    r = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path / "nope")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 2
    assert "traceback" not in r.stderr.lower()
