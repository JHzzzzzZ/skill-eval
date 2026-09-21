"""运行引擎（#1/#4/#5/#8/#12/#15 的数据源）。Seam: python trace_run.py <worktree> <prompt> [--skill <path>|--no-skill]

实跑模式调用 pi CLI --mode json --no-session 采集事件流，聚合为 trace JSON 输出。
--events <jsonl>  : 离线解析已采集的事件流（测试/重算用），跳过实跑
--build-only      : 只输出将要执行的命令，不运行（调试用）
trace 中的 triggered 判定：agent 是否执行了至少一次工具调用。
"""
import _console

_console.fix()

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _subprocess import kill_tree as _kill_tree  # 共享：杀进程树（Windows cmd 壳问题）

# 供测试/外部直接导入（test_trace_run.test_kill_tree_terminates_wrapped_process）
kill_tree = _kill_tree


class EventAggregator:
    """逐行聚合 pi --mode json 事件流。feed(line) 增量消费，result() 出 trace。

    early_exit=True（ADR-0007，触发评测专用）：检测到“已加载被测 skill”即置 stop，
    编排层据此 kill 子进程，不再花 token 跑完剩余回答。精确判定仍由 trigger_judge.py 兜底。

    停止条件（ADR-0007 修订）：pi 是渐进式披露——启动时只有 name/description 进上下文，
    SKILL.md 全文由模型决定使用时自己 read（实测 golden 全部第 0 步 read，但
    trig-shouldnot-3 实证过“先探索 4 步才加载”）。因此停止条件是首次
    args 指向被测 SKILL.md 的工具调用（= 加载事件本身），而不是任意首次工具调用：
    后者会在 agent 先探索后加载时把触发 run 误杀成假阴性，系统性低估 recall。
    skill_marker=None 时退回旧口径（任意首次工具调用即停），供无 skill 场景/离线测试。"""

    def __init__(self, early_exit: bool = False, skill_marker: str | None = None):
        self.early_exit = early_exit
        self.skill_marker = skill_marker
        self.stop = False
        self.steps, self.errors, self.usage_totals, self.answer_parts = [], [], [], []

    def feed(self, line: str):
        line = line.strip()
        if not line or self.stop:
            return
        try:
            ev = json.loads(line)
        except json.JSONDecodeError as e:
            self.errors.append(f"非法事件行: {e}")
            return
        if not isinstance(ev, dict) or "type" not in ev:
            self.errors.append(f"事件缺 type 字段: {line[:80]}")
            return
        t = ev["type"]
        if t == "tool_execution_start":
            raw_args = ev.get("args")
            args_hash = hashlib.sha256(json.dumps(raw_args, sort_keys=True).encode()).hexdigest()[:8]
            # 有界保留原始 args（ISS-3: 触发判定需要第一手证据）
            if isinstance(raw_args, str) and len(raw_args) > 200:
                raw_args = raw_args[:200]
            self.steps.append({"tool": ev.get("toolName", "?"), "args": raw_args, "args_hash": args_hash})
            if self.early_exit and self._hit_marker(raw_args):
                self.stop = True  # 加载事件 = 触发决定点：后续 token 不再花
                return
        elif t == "tool_execution_end" and ev.get("isError"):
            self.errors.append(f"工具错误 {ev.get('toolCallId')}: {str(ev.get('result'))[:200]}")
        if t == "turn_end":
            u = ev.get("usage") or {}
            self.usage_totals.append(u.get("totalTokens") or u.get("tokens", {}).get("total") or 0)
        elif t == "agent_end":
            for msg in ev.get("messages", []):
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    self.usage_totals.append((msg.get("usage") or {}).get("totalTokens") or 0)
                    for c in msg.get("content", []):
                        if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                            self.answer_parts.append(c["text"])  # ISS-3: 最终回答全文

    def _hit_marker(self, raw_args) -> bool:
        """停止条件：无 marker → 任意工具调用（旧口径）；有 marker → args 指向被测 SKILL.md。

        匹配用 args 的原始值（dict/list 递归取值）而不是 json.dumps——dumps 会把
        Windows 路径的 \\ 转义成 \\\\，归一化后与 marker 永远对不上（冒烟实测踩坑）。
        分隔符/连续斜杠/大小写归一（模型回读时可能用 / 或 \\）。"""
        if not self.skill_marker:
            return True

        def text(x):
            if isinstance(x, str):
                return x
            if isinstance(x, dict):
                return " ".join(text(v) for v in x.values())
            if isinstance(x, (list, tuple)):
                return " ".join(text(v) for v in x)
            return str(x)

        norm = lambda s: re.sub(r"/+", "/", str(s).replace("\\", "/")).lower()
        return norm(self.skill_marker) in norm(text(raw_args))

    def result(self) -> dict:
        trace = {
            "steps": self.steps,
            "tool_calls": len(self.steps),
            # usage 是累计值：取事件流最后一条；实跑模式 seconds 由 elapsed 计时
            "tokens": self.usage_totals[-1] if self.usage_totals else 0,
            "seconds": 0.0,  # 事件流无可靠时间戳时置 0，实跑模式下由 elapsed 计时填充
            "answer": "\n".join(self.answer_parts),
            "errors": self.errors,
            "triggered": bool(self.steps),
            "passed": not self.errors,
        }
        if self.early_exit and self.stop:
            trace["early_exit"] = True
        return trace


def parse_events(lines, early_exit: bool = False, skill_marker: str | None = None) -> dict:
    """把 pi --mode json 的事件流聚合为 trace。非法行计入 errors，不中断。"""
    agg = EventAggregator(early_exit, skill_marker)
    for line in lines:
        agg.feed(line)
        if agg.stop:
            break
    return agg.result()


DEFAULT_MODEL_ENV = "SKILL_EVAL_MODEL"  # 环境变量作默认模型，--model 显式覆盖


def build_args(worktree: str, prompt: str, skill=None,
               model: str = None, thinking: str = None) -> list:
    """pi CLI 命令。模型：--model 显式传参 > SKILL_EVAL_MODEL 环境变量 > pi 默认。

    skill 可为单个路径、路径列表或逗号分隔串；可重复传 --skill（补丁 B5）。
    始终带 --no-skills（关闭 skill 自动发现）：否则发现到的全局同名 skill 会
    屏蔽被测版本（故--skill 会被静默忽略），测量不再可复现（补丁 B3）。"""
    cmd = ["pi", "--mode", "json", "--no-session", "--no-skills"]
    if isinstance(skill, str):
        skills = [s.strip() for s in skill.split(",") if s.strip()]
    else:
        skills = [s for s in (skill or []) if s]
    for s in skills:
        # 解析为绝对路径：子进程 cwd=worktree，相对路径会按 worktree 解析
        # 导致 skill 静默不加载（实测冒烟踩坑）
        cmd += ["--skill", str(Path(s).resolve())]
    eff_model = model or os.environ.get(DEFAULT_MODEL_ENV)
    if eff_model:
        cmd += ["--model", eff_model]
    if thinking:
        cmd += ["--thinking", thinking]
    cmd += ["--", prompt]
    return cmd


def main():
    args = sys.argv[1:]
    worktree = prompt = model = thinking = out_path = None
    skills = []
    events_file = build_only = no_skill = early_exit = None
    # 位置参数形式（seam 声明）：trace_run.py <worktree> <prompt>
    positional = [a for i, a in enumerate(args) if a not in (
        "--events", "--build-only", "--worktree", "--prompt", "--skill", "--no-skill", "--early-exit",
        "--out", "--model", "--thinking", "--")
        and (i == 0 or args[i - 1] not in (
            "--events", "--worktree", "--prompt", "--skill", "--out", "--model", "--thinking"))
    ]
    i = 0
    while i < len(args):
        if args[i] == "--events":
            events_file = args[i + 1]; i += 2
        elif args[i] == "--build-only":
            build_only = True; i += 1
        elif args[i] == "--early-exit":
            early_exit = True; i += 1
        elif args[i] == "--worktree":
            worktree = args[i + 1]; i += 2
        elif args[i] == "--prompt":
            prompt = args[i + 1]; i += 2
        elif args[i] == "--skill":
            skills.append(args[i + 1]); i += 2
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
        print(json.dumps({"command": build_args(worktree, prompt, skills, model, thinking)}, ensure_ascii=False))
        return

    if events_file:
        # 离线解析同样应用 marker 口径（--skill 给了就该按加载事件停）
        marker = None
        if not no_skill and skills:
            marker = str(Path(skills[0]).resolve())
        trace = parse_events(Path(events_file).read_text(encoding="utf-8", errors="replace").splitlines(),
                             early_exit=bool(early_exit), skill_marker=marker)
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

    cmd = build_args(worktree, prompt, None if no_skill else skills, model, thinking)
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
        if not early_exit:
            # 全量模式：等 pi 跑完再聚合，保留 600s 超时语义
            proc = subprocess.run(cmd, cwd=worktree, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=600)
            elapsed = time.time() - t0
            trace = parse_events(proc.stdout.splitlines())
            trace["seconds"] = round(elapsed, 2)
            if proc.returncode != 0 and not trace["errors"]:
                trace["errors"].append(f"pi 退出码 {proc.returncode}: {proc.stderr[-300:]}")
        else:
            # ADR-0007 early-exit：流式消费事件流，检测到 skill 已使用即 kill
            proc = subprocess.Popen(cmd, cwd=worktree, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True,
                                    encoding="utf-8", errors="replace")
            killed_by_watchdog = threading.Event()
            watchdog = threading.Timer(600, lambda: (killed_by_watchdog.set(), _kill_tree(proc)))
            watchdog.start()
            agg = EventAggregator(early_exit=True,
                                  skill_marker=str(Path(skills[0]).resolve()) if skills else None)
            try:
                for line in proc.stdout:
                    agg.feed(line)
                    if agg.stop:
                        break
            finally:
                watchdog.cancel()
            timed_out = killed_by_watchdog.is_set()  # 不能用 watchdog.finished：cancel() 也会 set 它
            if agg.stop and proc.poll() is None:
                _kill_tree(proc)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                # taskkill 偶发失败/竞态：兜底再杀一次，wait 不设限就是下一个死锁
                _kill_tree(proc)
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    pass
            elapsed = time.time() - t0
            trace = agg.result()
            trace["seconds"] = round(elapsed, 2)
            stderr = ""
            try:
                if proc.poll() is not None:
                    stderr = proc.stderr.read() or ""
                else:
                    proc.stderr.close()  # 进程仍在：读会等 EOF 阻塞，宁丢诊断不错死锁
            except (OSError, ValueError):
                pass
            if timed_out:
                # watchdog 杀的树：明确报超时，而不是误导性的「pi 退出码」；保留部分 steps 作证据
                trace["errors"] = ["运行超时（600s）"]
                trace["passed"] = False
            elif proc.returncode not in (0, None) and not trace["errors"] and not agg.stop:
                trace["errors"].append(f"pi 退出码 {proc.returncode}: {stderr[-300:]}")
    except subprocess.TimeoutExpired:
        trace = parse_events([])
        trace["errors"] = ["运行超时（600s）"]
        trace["passed"] = False
    if out_path:
        Path(out_path).write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(trace, ensure_ascii=False))


if __name__ == "__main__":
    main()
