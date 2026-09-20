"""#10 过程可验证：trace 对照 SKILL.md，LLM 找无意义步骤。
Seam: python process.py --skill <SKILL.md> --trace <trace.json> [--model m] [--thinking t] [--events jsonl] [--out f]

--build-only 输出 prompt；--events 离线解析；无 JSON 回答保守判 fail（score 0）。
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from compare import extract_answer  # 同一 JSON 提取逻辑


def build_prompt(skill_text: str, trace: dict) -> str:
    steps = json.dumps(trace.get("steps", []), ensure_ascii=False, indent=2)
    errors = json.dumps(trace.get("errors", []), ensure_ascii=False)
    return (
        "你是 skill 运行过程审计员。对照 skill 的声明和实际执行 trace，找出\"无意义步骤\"：\n"
        "- 与声明的流程无关的操作\n"
        "- 重复已完成的工作（如重复下载/重复读取同一文件）\n"
        "- 超出步骤范围的探索性操作\n\n"
        f"skill 声明：\n{skill_text}\n\n"
        f"实际 trace 步骤：\n{steps}\n\n"
        f"运行报错：\n{errors_placeholder(trace)}\n\n"
        '只回答一个 JSON 对象：{"meaningless": ["步骤描述..."], "score": 0|1|2, "reason": "一句话"}\n'
        "score: 0=大量无意义步骤, 1=少量, 2=过程与声明一致。"
    )


def errors_placeholder(trace: dict) -> str:
    return json.dumps(trace.get("errors", []), ensure_ascii=False)


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
        print("usage: process.py --skill <SKILL.md> --trace <trace.json>", file=sys.stderr)
        sys.exit(2)
    try:
        skill_text = Path(skill_file).read_text(encoding="utf-8", errors="replace")
        trace = json.loads(Path(trace_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"read fail: {e}", file=sys.stderr)
        sys.exit(2)
    prompt = build_prompt(skill_text, trace)

    if build_only:
        print(json.dumps({"prompt": prompt}, ensure_ascii=False))
        return

    if events_file:
        answer = extract_answer(Path(events_file).read_text(encoding="utf-8", errors="replace"))
    else:
        cmd = ["pi", "-p", "--no-session", "--no-skills"]
        eff_model = model or os.environ.get("SKILL_EVAL_MODEL")
        if eff_model:
            cmd += ["--model", eff_model]
        if thinking:
            cmd += ["--thinking", thinking]
        cmd += ["--", prompt]
        resolved = shutil.which("pi") or shutil.which("pi.cmd") or shutil.which("pi.exe")
        if resolved:
            cmd[0] = resolved
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=300)
            answer = extract_answer(proc.stdout)
        except FileNotFoundError:
            answer = {"meaningless": [], "score": 0, "reason": "pi CLI 不存在"}
        except subprocess.TimeoutExpired:
            answer = {"meaningless": [], "score": 0, "reason": "过程审计超时（300s）"}
    if not isinstance(answer.get("score"), int):
        answer = {"meaningless": [], "score": 0,
                  "reason": f"LLM 回答无合法 score 字段: {str(answer)[:200]}"}
    if out_path:
        Path(out_path).write_text(json.dumps(answer, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(answer, ensure_ascii=False))


if __name__ == "__main__":
    main()
