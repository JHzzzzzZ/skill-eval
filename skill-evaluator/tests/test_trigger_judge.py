"""Seam: python scripts/trigger_judge.py --skill <SKILL.md> --trace <trace.json> [--model m] [--events jsonl]

ISS-2 产品化：LLM 判断 agent 行为/回答是否体现"使用了该 skill"，不再用"有无工具调用"近似。
输出: {"triggered": bool, "reason": str}
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "trigger_judge.py"


def run_judge(args: list):
    return subprocess.run(
        [sys.executable, str(SCRIPT.parent / "trigger_judge.py")] + args,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def make_skill(tmp_path, desc="帮用户评估 skill 质量，产出报告"):
    p = tmp_path / "SKILL.md"
    p.write_text(f"---\nname: s\ndescription: {desc}\n---\nbody", encoding="utf-8")
    return p


def make_trace(tmp_path, answer: str, steps=None):
    p = tmp_path / "trace.json"
    p.write_text(json.dumps({"steps": steps or [], "answer": answer}), encoding="utf-8")
    return p


# --- Slice 26: prompt 构造与解析 ---

def test_build_prompt(tmp_path):
    s = make_skill(tmp_path)
    t = make_trace(tmp_path, "我完成了评估报告")
    r = run_judge(["--build-only", "--skill", str(s), "--trace", str(t)])
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "帮用户评估" in out["prompt"]      # description 进 prompt
    assert "我完成了评估报告" in out["prompt"]  # 回答进 prompt


def test_parse_triggered(tmp_path):
    s = make_skill(tmp_path)
    t = make_trace(tmp_path, "回答")
    events = json.dumps({"type": "agent_end", "messages": [
        {"role": "assistant", "content": [{"type": "text", "text":
            '{"triggered": false, "reason": "回答与评估 skill 无关"}'}]},
    ]})
    f = tmp_path / "events.jsonl"; f.write_text(events, encoding="utf-8")
    out = json.loads(run_judge(["--skill", str(s), "--trace", str(t), "--events", str(f)]).stdout)
    assert out["triggered"] is False


def test_no_json_conservative_not_triggered(tmp_path):
    # 无法解析 → 保守判"未触发"并说明，不计入 precision 分母误报
    s = make_skill(tmp_path)
    t = make_trace(tmp_path, "闲聊")
    events = json.dumps({"type": "agent_end", "messages": [
        {"role": "assistant", "content": [{"type": "text", "text": "我觉得没触发"}]}]})
    f = tmp_path / "events.jsonl"; f.write_text(events, encoding="utf-8")
    out = json.loads(run_judge(["--skill", str(s), "--trace", str(t), "--events", str(f)]).stdout)
    assert out["triggered"] is False
    assert "reason" in out
