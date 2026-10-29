"""执行载体适配层（ADR-0034）。

当前只实现 pi：把一次 case 交给 trace_run.py（它负责 pi 事件流 → trace）。
Code Agent 留接口不实现；前端只显示 available() 里的项。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import _console

_console.fix()

import _config

SCRIPTS = Path(__file__).resolve().parent
TRACE_RUN = SCRIPTS / "trace_run.py"
IMPLEMENTED = ("pi",)


class HarnessError(RuntimeError):
    pass


def available() -> list:
    declared = _config.get_list("harness.available", ["pi"])
    out = [a for a in declared if isinstance(a, str) and a in IMPLEMENTED]
    return out or ["pi"]


def current(override: str | None = None) -> str:
    name = (override or _config.get_str("harness.adapter", "pi")).strip()
    if name not in IMPLEMENTED:
        raise HarnessError(f"执行载体 {name!r} 未实现；可用：{', '.join(available())}")
    return name


def _cmd(prompt: str, cwd: str | Path, *, skill_dir=None, no_skill=False,
         early_exit=False, model=None, thinking=None, out=None, guard_repo=None,
         extra=None) -> list:
    cmd = [sys.executable, str(TRACE_RUN), str(cwd), prompt]
    if no_skill:
        cmd.append("--no-skill")
    elif skill_dir:
        cmd += ["--skill", str(skill_dir)]
    if early_exit:
        cmd.append("--early-exit")
    if model:
        cmd += ["--model", model]
    if thinking:
        cmd += ["--thinking", thinking]
    if out:
        cmd += ["--out", str(out)]
    for repo in guard_repo or []:
        cmd += ["--guard-repo", str(repo)]
    cmd += list(extra or [])
    return cmd


def run_case(prompt: str, cwd: str | Path, *, skill_dir=None, no_skill=False,
             early_exit=False, model=None, thinking=None, out=None,
             guard_repo=None, timeout=None, build_only=False, adapter=None) -> dict:
    """跑一次 case，返回 trace dict；build_only 返回 {"command": [...]}。"""
    name = current(adapter)
    if name != "pi":
        raise HarnessError(f"执行载体 {name!r} 未实现")
    cmd = _cmd(prompt, cwd, skill_dir=skill_dir, no_skill=no_skill,
               early_exit=early_exit, model=model, thinking=thinking, out=out,
               guard_repo=guard_repo, extra=["--build-only"] if build_only else None)
    if build_only:
        return {"command": cmd}
    timeout = timeout or _config.get_int("run.timeout_s", 300)
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)
    if proc.returncode != 0:
        raise HarnessError(f"trace_run 退出码 {proc.returncode}: {proc.stderr[-800:]}")
    if out:
        try:
            return json.loads(Path(out).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HarnessError(f"读 trace 失败: {exc}") from exc
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise HarnessError(f"trace_run 输出不是 JSON: {proc.stdout[:300]}") from exc
