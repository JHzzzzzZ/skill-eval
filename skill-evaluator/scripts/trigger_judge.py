"""ISS-2 产品化：LLM 判定 agent 是否"使用了该 skill"。
Seam: python trigger_judge.py --skill <SKILL.md> --trace <trace.json> [--model m] [--events jsonl] [--out f]

回答/步骤解析不出 JSON → 保守判"未触发"（不计入误报），绝不静默判触发。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from compare import extract_answer  # 同一 JSON 提取逻辑
from _subprocess import ask_json, build_mode_args


def build_prompt(description: str, answer: str, steps) -> str:
    return (
        "你是 skill 触发判定员。判断 agent 的回答/行为是否体现\"实际使用了该 skill\"。\n"
        f"skill 的 description：{description}\n\n"
        f"agent 的最终回答：{answer[:2000]}\n\n"
        f"agent 执行的工具步骤：{json.dumps(steps, ensure_ascii=False)[:1500]}\n\n"
        '只回答一个 JSON 对象：{"triggered": true/false, "reason": "一句话理由"}\n'
        "判定标准：回答的内容/结构/行为确实符合 description 描述的功能才算 true；"
        "仅仅客气地提到 skill、或做了与 skill 无关的通用任务都算 false。"
    )


def main():
    args = sys.argv[1:]
    skill_file = trace_file = model = thinking = events_file = out_path = None
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
        elif args[i] == "--build-only":
            build_only = True; i += 1
        else:
            i += 1
    if not skill_file or not trace_file:
        print("usage: trigger_judge.py --skill <SKILL.md> --trace <trace.json>", file=sys.stderr)
        sys.exit(2)
    try:
        skill_text = Path(skill_file).read_text(encoding="utf-8", errors="replace")
        trace = json.loads(Path(trace_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"read fail: {e}", file=sys.stderr)
        sys.exit(2)
    m = None
    for line in skill_text.splitlines():
        if line.startswith("description:"):
            m = line.split(":", 1)[1].strip().strip('"')
            break
    prompt = build_prompt(m or skill_text[:300], trace.get("answer") or "", trace.get("steps") or [])

    if build_only:
        print(json.dumps({"prompt": prompt}, ensure_ascii=False))
        return

    if events_file:
        answer = extract_answer(Path(events_file).read_text(encoding="utf-8", errors="replace"))
    else:
        mode_args = build_mode_args(model or os.environ.get("SKILL_EVAL_MODEL"), thinking)
        try:
            events_text = ask_json(mode_args, prompt, timeout=300)
            answer = extract_answer(events_text)
        except FileNotFoundError:
            answer = {"triggered": False, "reason": "pi CLI 不存在"}
        except subprocess.TimeoutExpired:
            answer = {"triggered": False, "reason": "触发判定超时（300s）"}
    if not isinstance(answer.get("triggered"), bool):
        answer = {"triggered": False,
                  "reason": f"LLM 回答无合法 triggered 字段: {str(answer)[:200]}"}
    if out_path:
        Path(out_path).write_text(json.dumps(answer, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(answer, ensure_ascii=False))


if __name__ == "__main__":
    main()
