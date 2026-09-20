"""Seam: python scripts/compare.py --expect <file> --actual <file> [--model m] [--thinking t] [--out f]

#9 结果可验证：把评测集 case 的 expect（期望输出描述）和实际产出交给 LLM 判断是否匹配。
--build-only 只输出构造的 prompt（测试用）；--events <jsonl> 离线解析 LLM 回答。
stdout/落盘: {"match": bool, "score": 0|1|2, "reason": str}
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "compare.py"


def run_compare(args: list):
    return subprocess.run(
        [sys.executable, str(SCRIPT.parent / "compare.py")] + args,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


# --- Slice 20: prompt 构造 ---

def test_build_prompt(tmp_path):
    exp = tmp_path / "expect.txt"; exp.write_text("输出包含排序结果列表", encoding="utf-8")
    act = tmp_path / "actual.md"; act.write_text("排序结果：[1,2,3]", encoding="utf-8")
    r = run_compare(["--build-only", "--expect", str(exp), "--actual", str(act)])
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "输出包含排序结果列表" in out["prompt"]
    assert "排序结果：[1,2,3]" in out["prompt"]
    assert "JSON" in out["prompt"]  # 要求结构化回答


# --- Slice 21: 回答解析 ---

def test_parse_llm_answer(tmp_path):
    exp = tmp_path / "expect.txt"; exp.write_text("包含数字 4", encoding="utf-8")
    act = tmp_path / "actual.md"; act.write_text("2+2=4", encoding="utf-8")
    events = "\n".join([
        json.dumps({"type": "session", "id": "c1"}),
        json.dumps({"type": "agent_end", "messages": [
            {"role": "user", "content": []},
            {"role": "assistant", "content": [{"type": "text", "text": '```json\n{"match": true, "score": 2, "reason": "匹配"}\n```'}]},
        ]}),
    ])
    f = tmp_path / "events.jsonl"; f.write_text(events, encoding="utf-8")
    r = run_compare(["--expect", str(exp), "--actual", str(act), "--events", str(f)])
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["match"] is True and out["score"] == 2


def test_answer_without_json_is_error(tmp_path):
    exp = tmp_path / "e"; exp.write_text("x", encoding="utf-8")
    act = tmp_path / "a"; act.write_text("y", encoding="utf-8")
    events = json.dumps({"type": "agent_end", "messages": [
        {"role": "assistant", "content": [{"type": "text", "text": "我觉得差不多"}]}]})
    f = tmp_path / "events.jsonl"; f.write_text(events, encoding="utf-8")
    r = run_compare(["--expect", str(exp), "--actual", str(act), "--events", str(f)])
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["score"] == 0
    assert out["match"] is False
    assert "reason" in out


# --- 全仓复审 P2: 多 case 聚合 ---

def test_aggregate_multiple_cases(tmp_path):
    cdir = tmp_path / "compare"
    cdir.mkdir()
    for i, (m, s) in enumerate([(True, 2), (False, 0), (True, 1)]):
        (cdir / f"case{i}.json").write_text(
            json.dumps({"match": m, "score": s, "reason": "r"}), encoding="utf-8")
    r = run_compare(["--aggregate", str(cdir)])
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["mean_score"] == 1.0  # (2+0+1)/3
    assert out["match"] is True      # 2/3 匹配
    assert len(out["per_case"]) == 3


def test_aggregate_empty_dir(tmp_path):
    (tmp_path / "empty").mkdir()
    r = run_compare(["--aggregate", str(tmp_path / "empty")])
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["mean_score"] is None  # 无数据标 skipped 语义，不猜
