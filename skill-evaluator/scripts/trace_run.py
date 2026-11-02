"""运行引擎（#10/#13/#11/#12/#15/#16 的数据源）。Seam: python trace_run.py <沙箱目录> <prompt> [--skill <path>|--no-skill] [--adapter <name>] [--isolation host|docker]

实跑模式按 --adapter（默认 pi，--harness 为同义别名）构造命令、采集事件流，聚合为 trace JSON 输出。
--events <jsonl>  : 离线解析已采集的事件流（测试/重算用），跳过实跑；同样接受 --adapter
--build-only      : 只输出将要执行的命令，不运行（调试用）
--isolation docker: 用 sandbox.py 把 agent 命令包进一次性容器（workdir → /work）；
                    无容器运行时直接失败，不回退宿主。默认 host（旧测试与 T0/T1 行为不变）。
trace 中的 triggered 判定：agent 是否执行了至少一次工具调用。
"""
import _console

_console.fix()

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _subprocess import kill_tree as _kill_tree  # 共享：杀进程树（Windows cmd 壳问题）
import _config
import adapter_registry
import sandbox
from adapter_contract import ParseState, RunRequest, resolve_executable

ISOLATIONS = ("host", "docker")

# 供测试/外部直接导入（test_trace_run.test_kill_tree_terminates_wrapped_process）
kill_tree = _kill_tree


class EventAggregator:
    """逐行聚合 adapter 输出的规范化事件。feed(line) 增量消费，result() 出 trace。

    默认 adapter=pi：旧构造签名、trace 字段与早停口径保持不变（ADR-0007）。
    early_exit=True（触发评测专用）：收到 adapter 的 skill_loaded 事件即置 stop，编排层据此
    kill 子进程，不再花 token 跑完剩余回答；精确判定仍由 trigger_judge.py 兜底。
    skill_marker=None（没有加载被测 skill）→ adapter 不会报加载，恒不早停（跑完整）。

    停止条件是首次 args 指向被测 SKILL.md 的工具调用（= 加载事件本身），而不是任意首次
    工具调用：后者会在 agent 先探索后加载时把触发 run 误杀成假阴性，系统性低估 recall。
    判定逻辑由 adapter 用共享的 SkillLoadMatcher 完成，core 只认规范化事件。"""

    def __init__(self, early_exit: bool = False, skill_marker: str | None = None,
                 cwd: str | None = None, max_steps: int = 0, max_tokens: int = 0,
                 adapter=None):
        self.early_exit = early_exit
        self.skill_marker = skill_marker
        self.cwd = cwd  # 解析相对路径 token 的基准（实测：agent 用 `cd <dir> && cat SKILL.md`）
        self.max_steps = max(0, int(max_steps or 0))
        self.max_tokens = max(0, int(max_tokens or 0))
        self.adapter = adapter or adapter_registry.get("pi")
        self.state = ParseState(skill_marker=skill_marker, cwd=cwd, early_exit=bool(early_exit))
        self.limit_hit = None
        self.stop = False
        self.steps, self.errors, self.usage_totals, self.answer_parts = [], [], [], []

    def _limit(self, kind: str, value: int):
        self.limit_hit = f"{kind}上限（{value}）"
        self.stop = True

    def _usage(self, total) -> None:
        self.usage_totals.append(total or 0)
        if self.max_tokens and (total or 0) >= self.max_tokens:
            self._limit("token", self.max_tokens)

    def _step_entry(self, event: dict) -> dict:
        raw_args = event.get("args")
        args_hash = hashlib.sha256(json.dumps(raw_args, sort_keys=True).encode()).hexdigest()[:8]
        step = {"tool": event.get("tool") or "?", "args": raw_args, "args_hash": args_hash}
        # 有界保留原始 args（ISS-3：触发判定/过程审计要第一手证据）。旧版上限 200 字符：
        # 实测 pi 的 bash/read 工具 args 是 **dict**（{"command": ...}），该分支对它们从未生效，
        # 而字符串形态的工具参数会被切到 200。上限提到 2000 并在截断时留标记（ADR-0024）。
        args_limit = _config.get_int("run.args_max_chars", 2000)
        if isinstance(raw_args, str) and len(raw_args) > args_limit:
            step["args"] = raw_args[:args_limit]
            step["args_truncated"] = True
        return step

    def feed(self, line: str):
        if not line or not line.strip() or self.stop:
            return
        events = self.adapter.parse(line.strip(), self.state)
        step_added = False
        for event in events:
            if self.stop:
                break  # 加载事件/上限已定：同一条输出行里剩余事件不再计入
            kind = event.get("type")
            if kind == "step":
                self.steps.append(self._step_entry(event))
                step_added = True
            elif kind == "tool_error":
                self.errors.append(f"工具错误 {event.get('id')}: {event.get('message')}")
            elif kind == "usage":
                self._usage(event.get("tokens"))
            elif kind == "answer":
                self.answer_parts.append(event.get("text"))
            elif kind == "skill_loaded":
                if self.early_exit:
                    self.stop = True
            elif kind == "adapter_error":
                self.errors.append(str(event.get("message")))
        # 步数上限放在整行事件之后判：同行的加载事件优先（否则 marker 命中会被误记成 limit）
        if step_added and not self.stop and self.max_steps and len(self.steps) >= self.max_steps:
            self._limit("步数", self.max_steps)

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
        # adapter 特有字段（如 cost_usd）不得覆盖 core 的 trace 字段
        for key, value in (self.adapter.finalize(self.state) or {}).items():
            trace.setdefault(key, value)
        if self.limit_hit:
            trace["limit_hit"] = self.limit_hit
            trace["errors"].append(f"触发上限：{self.limit_hit}")
            trace["passed"] = False
        elif self.early_exit and self.stop:
            trace["early_exit"] = True
        return trace


def parse_events(lines, early_exit: bool = False, skill_marker: str | None = None,
                 cwd: str | None = None, max_steps: int = 0, max_tokens: int = 0,
                 adapter=None) -> dict:
    """把事件流聚合为 trace。非法行计入 errors，不中断。adapter 默认 pi。"""
    agg = EventAggregator(early_exit, skill_marker, cwd, max_steps, max_tokens, adapter=adapter)
    for line in lines:
        agg.feed(line)
        if agg.stop:
            break
    return agg.result()


DEFAULT_MODEL_ENV = "SKILL_EVAL_MODEL"  # 环境变量作默认模型，--model 显式覆盖

def _porcelain(repo: str) -> set:
    """仓库脏状态快照（越界写入检测的基准）。非 git 仓库/命令不可用 → 空集。"""
    p = subprocess.run(["git", "-C", repo, "status", "--porcelain"], capture_output=True,
                       text=True, errors="replace")
    if p.returncode != 0:
        return set()
    return {line.rstrip() for line in p.stdout.splitlines() if line.strip()}


def _revert_escape(repo: str, lines) -> list:
    """把越界写入退回原状（--guard-revert）：未跟踪→删；已跟踪的改/删→checkout 还原。"""
    done = []
    for line in lines:
        status, path = line[:2].strip(), line[3:].strip().strip('"')
        if not path:
            continue
        full = Path(repo) / path
        if status == "??":
            if full.is_dir():
                shutil.rmtree(full, ignore_errors=True)
            elif full.exists():
                full.unlink()
        else:
            subprocess.run(["git", "-C", repo, "checkout", "--", path], capture_output=True)
        done.append(path)
    return done


def build_args(worktree: str, prompt: str, skill=None,
               model: str = None, thinking: str = None, provider: str = None) -> list:
    """兼容封装：pi 命令构造（实现已移到 adapter_pi.build_args）。

    skill 可为单个路径、路径列表或逗号分隔串；可重复传 --skill（补丁 B5）。
    """
    if isinstance(skill, str):
        skills = [s.strip() for s in skill.split(",") if s.strip()]
    else:
        skills = [s for s in (skill or []) if s]
    request = RunRequest(prompt=prompt, cwd=worktree or "", skill_dir=skills[0] if skills else None,
                         skill_dirs=tuple(skills), model=model, thinking=thinking, provider=provider)
    return adapter_registry.get("pi").command(request)


def _make_request(worktree, prompt, skills, no_skill, model, thinking, provider,
                  early_exit=False, max_steps=0, max_tokens=0) -> RunRequest:
    """CLI 参数 → RunRequest（no_skill 时不把 skill 交给 adapter；env=None = 继承进程环境）。"""
    chosen = [] if no_skill else [s for s in skills or [] if s]
    return RunRequest(prompt=prompt or "", cwd=worktree or "",
                      skill_dir=chosen[0] if chosen else None, skill_dirs=tuple(chosen),
                      no_skill=bool(no_skill), model=model, thinking=thinking, provider=provider,
                      early_exit=bool(early_exit), max_steps=max_steps, max_tokens=max_tokens)


def _error_trace(adapter, message: str, version: str = "") -> dict:
    trace = parse_events([], adapter=adapter)
    trace["errors"] = [message]
    trace["passed"] = False
    trace["adapter"] = adapter.name
    trace["adapter_version"] = version
    return trace


def main():
    args = sys.argv[1:]
    worktree = prompt = model = thinking = provider = out_path = None
    adapter_name = adapter_registry.DEFAULT
    isolation = "host"
    skills = []
    guard_repos = []
    guard_revert = False
    events_file = build_only = no_skill = early_exit = None
    # 位置参数形式（seam 声明）：trace_run.py <worktree> <prompt>
    positional = [a for i, a in enumerate(args) if a not in (
        "--events", "--build-only", "--worktree", "--prompt", "--skill", "--no-skill", "--early-exit",
        "--out", "--model", "--thinking", "--provider", "--guard-repo", "--guard-revert",
        "--adapter", "--harness", "--isolation", "--")
        and (i == 0 or args[i - 1] not in (
            "--events", "--worktree", "--prompt", "--skill", "--out", "--model", "--thinking",
            "--provider", "--guard-repo", "--adapter", "--harness", "--isolation"))
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
        elif args[i] == "--provider":
            provider = args[i + 1]; i += 2
        elif args[i] == "--guard-repo":
            guard_repos.append(args[i + 1]); i += 2
        elif args[i] == "--guard-revert":
            guard_revert = True; i += 1
        elif args[i] in ("--adapter", "--harness"):  # --harness 是旧名，等价 --adapter
            adapter_name = args[i + 1]; i += 2
        elif args[i] == "--isolation":
            isolation = args[i + 1]; i += 2
        else:
            i += 1

    try:
        adapter = adapter_registry.get(adapter_name)
    except adapter_registry.UnknownAdapterError as exc:
        print(str(exc), file=sys.stderr)  # 不静默回退 pi：未知载体直接失败
        sys.exit(2)
    if isolation not in ISOLATIONS:
        print(f"--isolation 只支持 {'|'.join(ISOLATIONS)}，当前：{isolation}", file=sys.stderr)
        sys.exit(2)

    if not worktree and len(positional) >= 2:
        worktree, prompt = positional[0], positional[1]  # 位置参数形式（build-only/events 也认）

    if build_only:
        request = _make_request(worktree, prompt, skills, no_skill, model, thinking, provider)
        command = adapter.command(request)
        if isolation == "docker":
            if not worktree:
                print("docker 模式需要 worktree 目录", file=sys.stderr)
                sys.exit(2)
            command = sandbox.DockerBackend().wrap(command, worktree, prompt=request.prompt)
        print(json.dumps({"command": command}, ensure_ascii=False))
        return

    if events_file:
        # 离线解析同样应用 marker 口径（--skill 给了就该按加载事件停）
        marker = None
        if not no_skill and skills:
            marker = str(Path(skills[0]).resolve())
        trace = parse_events(Path(events_file).read_text(encoding="utf-8", errors="replace").splitlines(),
                             early_exit=bool(early_exit), skill_marker=marker, cwd=worktree,
                             max_steps=_config.get_int("run.trigger_max_steps", 30) if early_exit else 0,
                             max_tokens=_config.get_int("run.trigger_max_tokens", 120000) if early_exit else 0,
                             adapter=adapter)
        trace["model"] = model or os.environ.get(DEFAULT_MODEL_ENV)  # 宿主 pin：离线重算时至少记意图
        trace["adapter"] = adapter.name
        trace["adapter_version"] = ""  # 离线解析不探测二进制，避免测试依赖本机安装
        if out_path:
            _console.write_text(out_path, json.dumps(trace, ensure_ascii=False, indent=2))
        print(json.dumps(trace, ensure_ascii=False))
        return

    if not worktree or not prompt:
        print("usage: trace_run.py <worktree> <prompt> [--skill <path>|--no-skill] [--adapter <name>] "
              "[--isolation host|docker] [--events <jsonl>] [--out <file>]", file=sys.stderr)
        sys.exit(2)

    trigger_timeout = max(1, _config.get_int("run.trigger_timeout_s", 90))
    trigger_max_steps = max(0, _config.get_int("run.trigger_max_steps", 30))
    trigger_max_tokens = max(0, _config.get_int("run.trigger_max_tokens", 120000))
    request = _make_request(worktree, prompt, skills, no_skill, model, thinking, provider,
                            early_exit=bool(early_exit),
                            max_steps=trigger_max_steps if early_exit else 0,
                            max_tokens=trigger_max_tokens if early_exit else 0)
    available, version = adapter.available()
    if not available:
        print(json.dumps(_error_trace(adapter, version), ensure_ascii=False))
        return
    cmd = adapter.command(request)
    # Windows 上 pi 是 pi.cmd，subprocess 不解析 PATHEXT，需显式解析绝对路径。
    # docker 模式解析宿主路径没有意义：容器里用自己的 PATH。
    resolved = resolve_executable(adapter.binary)
    if resolved and isolation == "host":
        cmd[0] = resolved
    # 宿主 pin（ADR-0009 延伸）：trace 记录实际执行环境，事后可归因（同题方差分析/复现）
    host = {"model": model or os.environ.get(DEFAULT_MODEL_ENV),
            "adapter": adapter.name, "adapter_version": version, "platform": sys.platform,
            "isolation": isolation}
    if adapter.name == "pi":
        host["pi"] = version  # 旧字段保留：历史归因脚本按名读它
    if not Path(worktree).is_dir():
        print(json.dumps(_error_trace(adapter, f"worktree 目录不存在: {worktree}", version),
                         ensure_ascii=False))
        return
    backend = None
    if isolation == "docker":
        backend = sandbox.DockerBackend()
        status = backend.status()
        if not status.available:
            print(json.dumps(_error_trace(
                adapter, f"容器不可用：{status.summary()}；T2/T3 不回退宿主执行", version),
                ensure_ascii=False))
            return
        cmd = backend.wrap(cmd, worktree, prompt=request.prompt)
        host["sandbox"] = status.as_dict()
    try:
        adapter.prepare(request)  # 如 Claude 装项目级 skill；pi 无需安装
    except OSError as exc:
        print(json.dumps(_error_trace(adapter, f"adapter 准备失败（{adapter.name}）: {exc}", version),
                         ensure_ascii=False))
        return
    # docker 隔离只挂 workdir：宿主写入检测不适用，明确标记而不是静默跳过。
    guard_applicable = isolation == "host"
    guard_before = {}
    for repo in (guard_repos if guard_applicable else []):
        if Path(repo).is_dir():
            guard_before[repo] = _porcelain(repo)
        else:
            print(f"[guard] 仓库不存在，跳过: {repo}", file=sys.stderr)
    t0 = time.time()
    try:
        if not early_exit:
            # 全量模式：等载体跑完再聚合，保留 600s 超时语义
            proc = subprocess.run(cmd, cwd=worktree, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=600)
            elapsed = time.time() - t0
            trace = parse_events(proc.stdout.splitlines(), adapter=adapter)
            trace["seconds"] = round(elapsed, 2)
            if proc.returncode != 0 and not trace["errors"]:
                trace["errors"].append(f"{adapter.name} 退出码 {proc.returncode}: {proc.stderr[-300:]}")
                trace["passed"] = False
        else:
            # ADR-0007 early-exit：流式消费事件流，检测到 skill 已使用即 kill
            proc = subprocess.Popen(cmd, cwd=worktree, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True,
                                    encoding="utf-8", errors="replace")
            killed_by_watchdog = threading.Event()
            watchdog = threading.Timer(trigger_timeout,
                                       lambda: (killed_by_watchdog.set(), _kill_tree(proc)))
            watchdog.start()
            marker = str(Path(skills[0]).resolve()) if (skills and not no_skill) else None
            if marker and backend:
                marker = backend.rewrite_path(marker, worktree)  # 容器内看到的是 /work/...
            agg = EventAggregator(early_exit=True, skill_marker=marker,
                                  cwd=backend.workdir if backend else worktree,
                                  max_steps=trigger_max_steps,
                                  max_tokens=trigger_max_tokens, adapter=adapter)
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
                # watchdog 杀的树：明确报超时，而不是误导性的「退出码」；保留部分 steps 作证据
                trace["errors"] = [f"运行超时（{trigger_timeout}s）"]
                trace["passed"] = False
            elif proc.returncode not in (0, None) and not trace["errors"] and not agg.stop:
                trace["errors"].append(f"{adapter.name} 退出码 {proc.returncode}: {stderr[-300:]}")
                trace["passed"] = False
    except subprocess.TimeoutExpired:
        trace = parse_events([], adapter=adapter)
        trace["errors"] = ["运行超时（600s）"]
        trace["passed"] = False
    trace["adapter"] = adapter.name
    trace["adapter_version"] = version
    if guard_repos and not guard_applicable:
        trace["guard"] = {"applicable": False, "repos": [], "escaped_writes": [],
                          "reverted": [],
                          "reason": "docker 隔离：只挂载 workdir，不检测宿主写入"}
    if guard_before:
        # 越界写入检测（ADR-0027）：沙箱副本只限定 cwd，不限定写权限——实测 agent 会走出沙箱
        # 改真实仓库（README.md、judges/*.md、自己造 scratch/）。run 前后各拍一次 git 脏状态，
        # 新增条目即越界；--guard-revert 才回退（默认只报告，避免覆盖并行编辑）。
        guard = {"repos": [], "escaped_writes": [], "reverted": []}
        for repo, before in guard_before.items():
            after = _porcelain(repo)
            new = sorted(after - before)
            guard["repos"].append({"path": repo, "new_entries": len(new)})
            if new:
                guard["escaped_writes"].extend(f"{repo}: {line}" for line in new)
                if guard_revert:
                    guard["reverted"] = _revert_escape(repo, new)
                print(f"[guard] 检测到 {len(new)} 处越界写入：{new[:5]}", file=sys.stderr)
        trace["guard"] = guard
    if out_path:
        trace["host"] = host
        _console.write_text(out_path, json.dumps(trace, ensure_ascii=False, indent=2))
    trace["host"] = host
    print(json.dumps(trace, ensure_ascii=False))


if __name__ == "__main__":
    main()
