"""共享的 pi CLI 子进程执行壳（trace_run / compare / trigger_judge / process 共用）。

背景（实测缺陷，Windows）：
1. `pi` 在 npm 安装下是 pi.CMD（cmd.exe 壳）→ node 孙进程。proc.kill()/timeout 杀壳
   杀不到孙进程，孙进程持有 stdout/stderr 管道写端 → communicate()/read() 等不到 EOF
   永久阻塞（timeout 形同虚设）。taskkill /F /T 杀整棵树才能解开。
2. 长 prompt 走 argv 在 Windows 会 CreateProcess 失败（rc=1 空输出）→ prompt 一律走
   stdin（pi 无位置消息时从 stdin 读，语义等价）。
3. pi 的 LLM 裁决必须用 `--mode json` 事件流输出：compare.extract_answer 只认
   agent_end 事件，text 模式（-p）的输出永远解析不出 JSON（#1/#9/#10 假阴性根因）。

ask_json() 是三个判定脚本的唯一实跑入口：调用侧只管给 prompt，拿回事件流全文。
"""
import shutil
import subprocess
import sys

TIMEOUT_S = 300  # 可配：单次 LLM 裁决上限（reference.md § 可配参数）


def resolve_pi() -> str | None:
    """Windows 上 pi 是 pi.cmd，subprocess 不解析 PATHEXT，需显式解析绝对路径。"""
    return shutil.which("pi") or shutil.which("pi.cmd") or shutil.which("pi.exe")


def kill_tree(proc) -> None:
    """Windows 下 proc.kill 只杀 cmd.exe 包装层（pi.cmd），node 孙进程存活且持有
    stderr 管道写端 → 父进程 stderr.read() 等 EOF 永久阻塞（实测死锁）。
    taskkill /T 杀整棵树释放管道；POSIX 无包装层问题，直接 kill。"""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, timeout=10)
    else:
        proc.kill()


def build_mode_args(model: str | None, thinking: str | None) -> list:
    """判定调用的公共参数尾缀；模型优先级（显式 > SKILL_EVAL_MODEL）由调用方解析后传入。"""
    args = []
    if model:
        args += ["--model", model]
    if thinking:
        args += ["--thinking", thinking]
    return args


def run_cmd(cmd: list, prompt: str | None = None, timeout: int = TIMEOUT_S) -> tuple[int, str, str]:
    """跑一个子进程，返回 (returncode, stdout, stderr)。

    prompt 非 None 时经 stdin 传入（避开 Windows argv 长度/转义限制）；
    超时杀整棵进程树后重抛 TimeoutExpired（保守语义留给调用方标注），保证不悬挂。
    """
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    try:
        out, err = proc.communicate(input=prompt, timeout=timeout)
        return proc.returncode or 0, out or "", err or ""
    except subprocess.TimeoutExpired:
        # 杀整棵树（杀壳杀不到 node 孙进程 → 管道等不到 EOF），再重抛给调用方标注
        kill_tree(proc)
        try:
            proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            pass
        raise


def run_pi(mode_args: list, prompt: str | None = None, timeout: int = TIMEOUT_S) -> tuple[int, str, str]:
    """跑一次 pi（resolve_pi 失败抛 FileNotFoundError）。mode_args 见 build_mode_args。"""
    pi = resolve_pi()
    if not pi:
        raise FileNotFoundError("pi CLI 不存在")
    return run_cmd([pi, *mode_args], prompt=prompt, timeout=timeout)


def json_args(mode_args: list) -> list:
    """判定调用的完整参数：必须走 --mode json 事件流（extract_answer 只认事件流，
    text 模式恒解析失败——#1/#9/#10 假阴性根因，勿改回 -p）。"""
    return ["--mode", "json", "--no-session", "--no-skills", *mode_args]


def ask_json(mode_args: list, prompt: str, timeout: int = TIMEOUT_S) -> str:
    """LLM 裁决专用形态：--mode json 事件流，prompt 走 stdin，返回事件流全文。"""
    _rc, out, _err = run_pi(json_args(mode_args), prompt=prompt, timeout=timeout)
    return out
