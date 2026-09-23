"""Seam: python scripts/process.py --skill <SKILL.md> --trace <trace.json> [--model m] [--events jsonl]

#10 过程可验证：把 skill.md（声明）与 trace（实际步骤）对照，LLM 找无意义步骤。
输出: {"meaningless": [...], "score": 0|1|2, "reason"}
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "process.py"


def run_process(args: list):
    return subprocess.run(
        [sys.executable, str(SCRIPT.parent / "process.py")] + args,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def make_skill(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "SKILL.md"
    p.write_text(body, encoding="utf-8")
    return p


def make_trace(tmp_path: Path, steps: list) -> Path:
    p = tmp_path / "trace.json"
    p.write_text(json.dumps({"steps": steps}), encoding="utf-8")
    return p


# --- Slice 22: prompt 构造 ---

def test_build_prompt(tmp_path):
    s = make_skill(tmp_path, "## 步骤\n1. 读文件\n2. 写报告\n")
    t = make_trace(tmp_path, [{"tool": "read", "args_hash": "a1"}])
    r = run_process(["--build-only", "--skill", str(s), "--trace", str(t)])
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "读文件" in out["prompt"]
    assert '"tool": "read"' in out["prompt"]
    assert "JSON" in out["prompt"]


# --- Slice 23: 回答解析 ---

def test_parse_answer(tmp_path):
    s = make_skill(tmp_path, "body")
    t = make_trace(tmp_path, [{"tool": "read", "args_hash": "a"}])
    events = json.dumps({"type": "agent_end", "messages": [
        {"role": "assistant", "content": [{"type": "text", "text":
            '{"meaningless": [], "score": 2, "reason": "步骤均与声明一致"}'}]},
    ]})
    f = tmp_path / "events.jsonl"; f.write_text(events, encoding="utf-8")
    r = run_process(["--skill", str(s), "--trace", str(t), "--events", str(f)])
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["score"] == 2 and out["meaningless"] == []


def test_answer_not_json_conservative_fail(tmp_path):
    s = make_skill(tmp_path, "body")
    t = make_trace(tmp_path, [])
    events = json.dumps({"type": "agent_end", "messages": [
        {"role": "assistant", "content": [{"type": "text", "text": "没发现啥问题"}]}]})
    f = tmp_path / "events.jsonl"; f.write_text(events, encoding="utf-8")
    out = json.loads(run_process(["--skill", str(s), "--trace", str(t),
                                  "--events", str(f)]).stdout)
    assert out["score"] == 0 and "reason" in out


# --- #10 审计范围：用例范围必须进 prompt（ADR-0024）---

def test_case_scope_included_in_prompt(tmp_path):
    s = make_skill(tmp_path, "## 流程\n1. 前置自检\n2. 静态检查\n")
    t = make_trace(tmp_path, [{"tool": "bash", "args_hash": "a1"}])
    case = tmp_path / "c1.json"
    case.write_text(json.dumps({"prompt": "只跑 T0 静态档", "expect": "给出报告路径与结论"},
                               ensure_ascii=False), encoding="utf-8")
    r = run_process(["--build-only", "--skill", str(s), "--trace", str(t), "--case", str(case)])
    assert r.returncode == 0, r.stderr
    prompt = json.loads(r.stdout)["prompt"]
    assert "只跑 T0 静态档" in prompt and "给出报告路径与结论" in prompt
    assert "用例范围" in prompt and "不算无意义" in prompt   # 判据被限定在用例范围内


def test_without_case_prompt_still_works(tmp_path):
    s = make_skill(tmp_path, "## 流程\n1. 前置自检\n")
    t = make_trace(tmp_path, [{"tool": "bash", "args_hash": "a1"}])
    r = run_process(["--build-only", "--skill", str(s), "--trace", str(t)])
    assert r.returncode == 0, r.stderr
    assert "本次运行的**用例范围**" not in json.loads(r.stdout)["prompt"]   # 未给 --case 时不注入用例段
