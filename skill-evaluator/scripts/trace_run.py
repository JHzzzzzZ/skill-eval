"""运行引擎（#1/#4/#5/#8/#12/#15 的数据源）。Seam: python trace_run.py <worktree> <prompt> [--skill <path>|--no-skill]

实跑模式调用 pi CLI --mode json --no-session 采集事件流，聚合为 trace JSON 输出。
--events <jsonl>  : 离线解析已采集的事件流（测试/重算用），跳过实跑
--build-only      : 只输出将要执行的命令，不运行（调试用）
trace 中的 triggered 判定：agent 是否执行了至少一次工具调用。
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def parse_events(lines) -> dict:
    """把 pi --mode json 的事件流聚合为 trace。非法行计入 errors，不中断。"""
    steps, errors, usage_totals, answer_parts = [], [], [], []
    t0 = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError as e:
            errors.append(f"非法事件行: {e}")
            continue
        if not isinstance(ev, dict) or "type" not in ev:
            errors.append(f"事件缺 type 字段: {line[:80]}")
            continue
        t = ev["type"]
        if t == "tool_execution_start":
            raw_args = ev.get("args")
            args_hash = hashlib.sha256(json.dumps(raw_args, sort_keys=True).encode()).hexdigest()[:8]
            # 有界保留原始 args（ISS-3: 触发判定需要第一手证据）
            if isinstance(raw_args, str) and len(raw_args) > 200:
                raw_args = raw_args[:200]
            steps.append({"tool": ev.get("toolName", "?"), "args": raw_args, "args_hash": args_hash})
        elif t == "tool_execution_end" and ev.get("isError"):
            errors.append(f"工具错误 {ev.get('toolCallId')}: {str(ev.get('result'))[:200]}")
        elif t == "turn_end" and not t0:
            t0 = time.time()
        if t == "turn_end":
            u = ev.get("usage") or {}
            usage_totals.append(u.get("totalTokens") or u.get("tokens", {}).get("total") or 0)
        elif t == "agent_end":
            for msg in ev.get("messages", []):
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    usage_totals.append((msg.get("usage") or {}).get("totalTokens") or 0)
                    for c in msg.get("content", []):
                        if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                            answer_parts.append(c["text"])  # ISS-3: 最终回答全文
    # usage 是累计值：取最后一条非零
    return {
        "steps": steps,
        "tool_calls": len(steps),
        "tokens": usage_totals[-1] if usage_totals else 0,
        "seconds": 0.0,  # 事件流无可靠时间戳时置 0，实跑模式下由 elapsed 计时填充
        "answer": "\n".join(answer_parts),
        "errors": errors,
        "triggered": bool(steps),
        "passed": not errors,
    }


DEFAULT_MODEL_ENV = "SKILL_EVAL_MODEL"  # 环境变量作默认模型，--model 显式覆盖


def build_args(worktree: str, prompt: str, skill: str = None,
               model: str = None, thinking: str = None) -> list:
    """pi CLI 命令。模型：--model 显式传参 > SKILL_EVAL_MODEL 环境变量 > pi 默认。"""
    cmd = ["pi", "--mode", "json", "--no-session"]
    if skill:
        cmd += ["--skill", skill]
    else:
        cmd += ["--no-skills"]
    eff_model = model or os.environ.get(DEFAULT_MODEL_ENV)
    if eff_model:
        cmd += ["--model", eff_model]
    if thinking:
        cmd += ["--thinking", thinking]
    cmd += ["--", prompt]
    return cmd


def main():
    args = sys.argv[1:]
    worktree = prompt = skill = model = thinking = out_path = None
    events_file = build_only = no_skill = None
    # 位置参数形式（seam 声明）：trace_run.py <worktree> <prompt>
    positional = [a for i, a in enumerate(args) if a not in (
        "--events", "--build-only", "--worktree", "--prompt", "--skill", "--no-skill",
        "--out", "--model", "--thinking")
        and (i == 0 or args[i - 1] not in (
            "--events", "--worktree", "--prompt", "--skill", "--out", "--model", "--thinking"))
    ]
    i = 0
    while i < len(args):
        if args[i] == "--events":
            events_file = args[i + 1]; i += 2
        elif args[i] == "--build-only":
            build_only = True; i += 1
        elif args[i] == "--worktree":
            worktree = args[i + 1]; i += 2
        elif args[i] == "--prompt":
            prompt = args[i + 1]; i += 2
        elif args[i] == "--skill":
            skill = args[i + 1]; i += 2
        elif args[i] == "--no-skill":
            no_skill = True; i += 1
        elif args[i] == "--out":
            out_path = args[i + 1]; i += 2
        elif args[i] == "--model":
            model = args[i + 1]; i += 2
        elif args[i] == "--thinking":
            thinking = args[i + 1]; i += 2
        else:
            i += 1

    if build_only:
        print(json.dumps({"command": build_args(worktree, prompt, skill, model, thinking)}, ensure_ascii=False))
        return

    if events_file:
        trace = parse_events(Path(events_file).read_text(encoding="utf-8", errors="replace").splitlines())
        if out_path:
            Path(out_path).write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(trace, ensure_ascii=False))
        return

    if not worktree and len(positional) >= 2:
        worktree, prompt = positional[0], positional[1]  # 位置参数形式
    if not worktree or not prompt:
        print("usage: trace_run.py <worktree> <prompt> [--skill <path>|--no-skill] [--events <jsonl>] [--out <file>]",
              file=sys.stderr)
        sys.exit(2)

    cmd = build_args(worktree, prompt, None if no_skill else skill, model, thinking)
    # Windows 上 pi 是 pi.cmd，subprocess 不解析 PATHEXT，需显式解析绝对路径
    resolved = shutil.which("pi") or shutil.which("pi.cmd") or shutil.which("pi.exe")
    if not resolved:
        trace = parse_events([])
        trace["errors"] = ["pi CLI 不存在：请先安装 pi（npm i -g @earendil-works/pi-coding-agent）"]
        trace["passed"] = False
        print(json.dumps(trace, ensure_ascii=False))
        return
    cmd[0] = resolved
    if not Path(worktree).is_dir():
        trace = parse_events([])
        trace["errors"] = [f"worktree 目录不存在: {worktree}"]
        trace["passed"] = False
        print(json.dumps(trace, ensure_ascii=False))
        return
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=worktree, capture_output=True, text=True, timeout=600)
        elapsed = time.time() - t0
        trace = parse_events(proc.stdout.splitlines())
        trace["seconds"] = round(elapsed, 2)
        if proc.returncode != 0 and not trace["errors"]:
            trace["errors"].append(f"pi 退出码 {proc.returncode}: {proc.stderr[-300:]}")
    except subprocess.TimeoutExpired:
        trace = parse_events([])
        trace["errors"] = ["运行超时（600s）"]
        trace["passed"] = False
    if out_path:
        Path(out_path).write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(trace, ensure_ascii=False))


if __name__ == "__main__":
    main()
