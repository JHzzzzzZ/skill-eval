"""Seam: python scripts/trigger_judge.py --skill <SKILL.md> --trace <trace.json> [--model m] [--events jsonl]
                                                                    [--rubric f] [--threshold 0.5]

ISS-2 产品化：#1 二级精判 = LLM 按 judges/trigger.md rubric 逐项判定（score = pass 项数/总项数，0~1），
triggered = score >= 阈值（默认 0.5）。输出: {"triggered", "score", "threshold", "items", "evidence", "reason"}。
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


def make_trace(tmp_path, answer: str, steps=None, early_exit=False):
    p = tmp_path / "trace.json"
    data = {"steps": steps or [], "answer": answer}
    if early_exit:
        data["early_exit"] = True
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def make_events(tmp_path, text: str):
    f = tmp_path / "events.jsonl"
    f.write_text(json.dumps({"type": "agent_end", "messages": [
        {"role": "assistant", "content": [{"type": "text", "text": text}]}]}), encoding="utf-8")
    return f


def rubric_payload(passes, evidence=None, score=None):
    items = [{"name": f"检查点{i+1}", "pass": p, "quote": f"证据{i+1}"} for i, p in enumerate(passes)]
    return {"items": items,
            "score": score if score is not None else round(sum(passes) / len(passes), 2),
            "evidence": evidence if evidence is not None else [], "reason": "理由"}


# --- prompt 构造 ---

def test_build_prompt(tmp_path):
    s = make_skill(tmp_path)
    t = make_trace(tmp_path, "我完成了评估报告")
    r = run_judge(["--build-only", "--skill", str(s), "--trace", str(t)])
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "帮用户评估" in out["prompt"]      # description 进 prompt
    assert "我完成了评估报告" in out["prompt"]  # 回答进 prompt
    assert "加载证据" in out["prompt"]         # judges/trigger.md rubric 进 prompt


def test_missing_rubric_is_error(tmp_path):
    s = make_skill(tmp_path)
    t = make_trace(tmp_path, "回答")
    r = run_judge(["--build-only", "--skill", str(s), "--trace", str(t),
                   "--rubric", str(tmp_path / "nope.md")])
    assert r.returncode != 0
    assert "rubric" in r.stderr


# --- rubric 契约解析与布尔化 ---

def test_score_derived_from_items_and_triggered(tmp_path):
    # 模型自报 score 0.99 也不认：score 一律由 items 推导（4 项 3 过 = 0.75 ≥ 0.5）
    s = make_skill(tmp_path)
    t = make_trace(tmp_path, "回答")
    payload = rubric_payload([True, True, True, False], score=0.99)
    out = json.loads(run_judge(["--skill", str(s), "--trace", str(t),
                                "--events", str(make_events(tmp_path, json.dumps(payload, ensure_ascii=False)))]).stdout)
    assert out["score"] == 0.75 and out["triggered"] is True
    assert len(out["items"]) == 4 and out["threshold"] == 0.5


def test_score_below_threshold_not_triggered(tmp_path):
    s = make_skill(tmp_path)
    t = make_trace(tmp_path, "回答")
    payload = rubric_payload([True, False, False, False])
    out = json.loads(run_judge(["--skill", str(s), "--trace", str(t),
                                "--events", str(make_events(tmp_path, json.dumps(payload, ensure_ascii=False)))]).stdout)
    assert out["score"] == 0.25 and out["triggered"] is False


def test_threshold_override(tmp_path):
    s = make_skill(tmp_path)
    t = make_trace(tmp_path, "回答")
    payload = rubric_payload([True, False, False, False])
    out = json.loads(run_judge(["--skill", str(s), "--trace", str(t), "--threshold", "0.2",
                                "--events", str(make_events(tmp_path, json.dumps(payload, ensure_ascii=False)))]).stdout)
    assert out["score"] == 0.25 and out["triggered"] is True and out["threshold"] == 0.2


def test_evidence_autofilled_from_failed_items(tmp_path):
    # score<1 而模型没给 evidence → 用失败项 quote 补齐，不让校验失败吞掉裁决
    s = make_skill(tmp_path)
    t = make_trace(tmp_path, "回答")
    payload = rubric_payload([True, True, False, False], evidence=[])
    out = json.loads(run_judge(["--skill", str(s), "--trace", str(t),
                                "--events", str(make_events(tmp_path, json.dumps(payload, ensure_ascii=False)))]).stdout)
    assert out["score"] == 0.5 and out["triggered"] is True
    assert out["evidence"] and "检查点3" in out["evidence"][0]


def test_legacy_flat_contract_rejected(tmp_path):
    # 旧 {"triggered": bool} 无 items → 严格拒绝，保守判未触发（防两套契约混用）
    s = make_skill(tmp_path)
    t = make_trace(tmp_path, "回答")
    events = make_events(tmp_path, '{"triggered": true, "reason": "看起来用了"}')
    out = json.loads(run_judge(["--skill", str(s), "--trace", str(t), "--events", str(events)]).stdout)
    assert out["triggered"] is False and out["score"] is None


def test_no_json_conservative_not_triggered(tmp_path):
    # 无法解析 → 保守判"未触发"并说明，不计入 precision 分母误报
    s = make_skill(tmp_path)
    t = make_trace(tmp_path, "闲聊")
    events = make_events(tmp_path, "我觉得没触发")
    out = json.loads(run_judge(["--skill", str(s), "--trace", str(t), "--events", str(events)]).stdout)
    assert out["triggered"] is False and out["score"] is None
    assert "reason" in out


# --- ADR-0007 修订同步：判定口径 = 加载即触发，不要求任务完成 ---

def test_prompt_early_exit_semantics(tmp_path):
    # 回归：旧 prompt 要求"实际完成登记"→ early-exit 截断的 should 全判 false（10/10 假阴性）
    s = make_skill(tmp_path)
    t = make_trace(tmp_path, "", steps=[{"tool": "read", "args": {"path": "SKILL.md"}}], early_exit=True)
    out = json.loads(run_judge(["--build-only", "--skill", str(s), "--trace", str(t)]).stdout)
    prompt = out["prompt"]
    assert "不要求任务实际完成" in prompt          # 加载即触发口径（rubric）
    assert "early-exit 截断" in prompt            # 截断告知，按已执行步骤判定
    assert "（空）" in prompt                      # 空回答显式标注而非静默传空
