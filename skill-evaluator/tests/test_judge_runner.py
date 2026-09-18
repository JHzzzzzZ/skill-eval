"""Seam: python scripts/judge_runner.py --validate <judge输出.json> -> stdout JSON

LLM 评审（rubric 在 judges/*.md）产出 JSON：{"score": 0|1|2, "evidence": [...], "reason": str}
校验器保证入报告前的结构正确；LLM 调用由主 agent 完成，脚本只做契约校验。
stdout: {"valid": bool, "errors": [...]}
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "judge_runner.py"


def run_validate(tmp_path: Path, payload) -> dict:
    p = tmp_path / "judge_out.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--validate", str(p)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"script failed: {r.stderr}"
    return json.loads(r.stdout)


# --- Slice 8: 合法输出 ---

def test_valid_judge_output(tmp_path):
    payload = {"score": 1, "evidence": ["引用的原文"], "reason": "有部分冗余"}
    assert run_validate(tmp_path, payload)["valid"] is True


# --- Slice 9: 非法输出 ---

def test_score_out_of_range(tmp_path):
    out = run_validate(tmp_path, {"score": 3, "evidence": [], "reason": "x"})
    assert out["valid"] is False
    assert any("score" in e for e in out["errors"])


def test_evidence_must_be_nonempty_strings(tmp_path):
    # score=2 可以 evidence 为空；score<2 时 evidence 必须非空且全是字符串
    ok = run_validate(tmp_path, {"score": 2, "evidence": [], "reason": "无冗余"})
    assert ok["valid"] is True
    bad = run_validate(tmp_path, {"score": 1, "evidence": [], "reason": "x"})
    assert bad["valid"] is False
    bad2 = run_validate(tmp_path, {"score": 1, "evidence": [123], "reason": "x"})
    assert bad2["valid"] is False


def test_missing_reason(tmp_path):
    out = run_validate(tmp_path, {"score": 0, "evidence": ["a"]})
    assert out["valid"] is False
    assert any("reason" in e for e in out["errors"])


def test_malformed_json_file(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not json", encoding="utf-8")
    out = run_validate(tmp_path, {})  # 占位，下面直接跑
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--validate", str(p)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["valid"] is False


# --- Slice 30: 文件缺失受控失败（全仓复审 P2）---

def test_missing_file_controlled(tmp_path):
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--validate", str(tmp_path / "nope.json")],
        capture_output=True, text=True)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["valid"] is False
    assert "errors" in out
