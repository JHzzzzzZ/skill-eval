"""ISS-2 产品化：LLM 按 judges/trigger.md rubric 精判 agent 是否"使用了该 skill"（#1 二级精判）。
Seam: python trigger_judge.py --skill <SKILL.md> --trace <trace.json> [--model m] [--thinking t]
                              [--events jsonl] [--out f] [--rubric f] [--threshold 0.5]

输出：judge 契约（#5 逐项计分）+ 布尔化结果
{"triggered": bool, "score": 0~1, "threshold": float, "items": [...], "evidence": [...], "reason": str}
score 一律由 items 推导（pass 项数/总项数），judge_runner.validate 强制校验；
triggered = score >= threshold（默认 0.5，可配 TRIGGER_PASS_SCORE / --threshold）。

解析不出合法契约 → 保守判"未触发"（不计入误报），绝不静默判触发。
不再兼容旧扁平 {"triggered": bool} 契约：rubric 输出必须带 items，混用会让打分不可复现。
"""
import _console

_console.fix()

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from compare import extract_answer  # 同一 JSON 提取逻辑（花括号配对，支持嵌套 items[]）
from judge_runner import score_of_items, validate  # #5 同一计分/校验口径
from _subprocess import ask_json, build_mode_args

DEFAULT_THRESHOLD = 0.5  # 可配参数（reference.md § 可配参数）
DEFAULT_RUBRIC = Path(__file__).parent.parent / "judges" / "trigger.md"


def build_prompt(rubric: str, description: str, answer: str, steps, early_exit: bool = False) -> str:
    truncated = (
        "注意：本次运行被 early-exit 截断（检测到加载 skill 即停，最终回答为空），"
        "按已执行步骤判定。\n\n" if early_exit else ""
    )
    return (
        f"{rubric}\n\n"
        "===== 本次判定材料 =====\n"
        f"skill 的 description：{description}\n\n"
        f"agent 的最终回答：{(answer[:2000] or '（空）')}\n\n"
        f"agent 执行的工具步骤：{json.dumps(steps, ensure_ascii=False)[:1500]}\n\n"
        f"{truncated}"
    )


def normalize_payload(raw) -> tuple:
    """LLM 原始输出 → (judge 契约 | None, errors)。

    score 一律由 items 推导（#5），不信任模型自报分值；score<1 时缺失的
    evidence 用失败项自身的 quote 补齐（引用不凭空编造）。结构/契约不合法 → None。
    """
    if not isinstance(raw, dict):
        return None, ["顶层不是 JSON 对象"]
    items = raw.get("items")
    if not isinstance(items, list) or not items:
        return None, ["items 缺失或为空"]
    for i, it in enumerate(items):
        if not isinstance(it, dict) or not isinstance(it.get("name"), str) or not it["name"].strip() \
                or not isinstance(it.get("pass"), bool) \
                or not isinstance(it.get("quote"), str) or not it["quote"].strip():
            return None, [f"items[{i}] 缺非空 name/quote 或布尔 pass"]
    score = score_of_items(items)
    evidence = raw.get("evidence")
    if not (isinstance(evidence, list) and all(isinstance(e, str) for e in evidence)):
        evidence = []
    if score < 1 and not evidence:
        evidence = [f"{it['name']}：{it['quote']}" for it in items if not it["pass"]]
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = "（LLM 未给理由）"
    payload = {"items": items, "score": score, "evidence": evidence, "reason": reason}
    errors = validate(payload)
    return (payload if not errors else None), errors


def parse_threshold(cli_value=None) -> float:
    """优先级：--threshold > TRIGGER_PASS_SCORE 环境变量 > 默认 0.5；非法值回落默认。"""
    raw = cli_value if cli_value is not None else os.environ.get("TRIGGER_PASS_SCORE")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_THRESHOLD
    try:
        v = float(raw)
    except ValueError:
        return DEFAULT_THRESHOLD
    return v if 0.0 <= v <= 1.0 else DEFAULT_THRESHOLD


def main():
    args = sys.argv[1:]
    skill_file = trace_file = model = thinking = events_file = out_path = None
    rubric_file = threshold_arg = None
    build_only = False
    i = 0
    while i < len(args):
        if args[i] == "--skill":
            skill_file = args[i + 1]; i += 2
        elif args[i] == "--trace":
            trace_file = args[i + 1]; i += 2
        elif args[i] == "--model":
            model = args[i + 1]; i += 2
        elif args[i] == "--thinking":
            thinking = args[i + 1]; i += 2
        elif args[i] == "--events":
            events_file = args[i + 1]; i += 2
        elif args[i] == "--out":
            out_path = args[i + 1]; i += 2
        elif args[i] == "--rubric":
            rubric_file = args[i + 1]; i += 2
        elif args[i] == "--threshold":
            threshold_arg = args[i + 1]; i += 2
        elif args[i] == "--build-only":
            build_only = True; i += 1
        else:
            i += 1
    if not skill_file or not trace_file:
        print("usage: trigger_judge.py --skill <SKILL.md> --trace <trace.json> "
              "[--rubric <rubric.md>] [--threshold 0.5]", file=sys.stderr)
        sys.exit(2)
    try:
        skill_text = Path(skill_file).read_text(encoding="utf-8", errors="replace")
        trace = json.loads(Path(trace_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"read fail: {e}", file=sys.stderr)
        sys.exit(2)
    rubric_path = Path(rubric_file) if rubric_file else DEFAULT_RUBRIC
    try:
        rubric = rubric_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"rubric 读取失败: {e}", file=sys.stderr)
        sys.exit(2)

    m = None
    for line in skill_text.splitlines():
        if line.startswith("description:"):
            m = line.split(":", 1)[1].strip().strip('"')
            break
    prompt = build_prompt(rubric, m or skill_text[:300], trace.get("answer") or "",
                          trace.get("steps") or [], bool(trace.get("early_exit")))

    if build_only:
        print(json.dumps({"prompt": prompt}, ensure_ascii=False))
        return

    if events_file:
        raw = extract_answer(Path(events_file).read_text(encoding="utf-8", errors="replace"))
    else:
        mode_args = build_mode_args(model or os.environ.get("SKILL_EVAL_MODEL"), thinking)
        try:
            events_text = ask_json(mode_args, prompt, timeout=300)
            raw = extract_answer(events_text)
        except FileNotFoundError:
            raw = {"triggered": False, "reason": "pi CLI 不存在"}
        except subprocess.TimeoutExpired:
            raw = {"triggered": False, "reason": "触发判定超时（300s）"}

    threshold = parse_threshold(threshold_arg)
    payload, errors = normalize_payload(raw)
    if payload is None:
        out = {"triggered": False, "score": None, "threshold": threshold,
               "items": [], "evidence": [],
               "reason": f"LLM 输出不满足 rubric 契约，保守判未触发：{'; '.join(errors)[:200]}"}
    else:
        out = {"triggered": payload["score"] >= threshold, "threshold": threshold, **payload}
    if out_path:
        Path(out_path).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
