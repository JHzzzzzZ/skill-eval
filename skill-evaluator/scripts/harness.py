"""执行载体适配层（ADR-0034）。

载体 = adapter 注册表里的一项（pi / claude-code…）：本层只把一次 case 交给
trace_run.py（它负责 adapter 命令与事件流 → trace），不再硬编码具体 agent。
配置里的 `--harness` 是 `--adapter` 的旧名，两者等价。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import _console

_console.fix()

import _config
import adapter_registry

SCRIPTS = Path(__file__).resolve().parent
TRACE_RUN = SCRIPTS / "trace_run.py"
DEFAULT = adapter_registry.DEFAULT


class HarnessError(RuntimeError):
    pass


def available() -> list:
    """config 声明的 adapter 中，已注册且二进制可用的项（前端只显示这些）。"""
    declared = _config.get_list("harness.available", adapter_registry.names())
    usable = adapter_registry.available()
    return [name for name in declared if isinstance(name, str) and name in usable]


def current(override: str | None = None) -> str:
    name = (override or _config.get_str("harness.adapter", DEFAULT)).strip()
    try:
        adapter_registry.get(name)
    except adapter_registry.UnknownAdapterError as exc:
        raise HarnessError(f"执行载体 {name!r} 未实现；可用：{', '.join(adapter_registry.names())}") from exc
    return name


def _cmd(adapter: str, prompt: str, cwd: str | Path, *, skill_dir=None, no_skill=False,
         early_exit=False, model=None, thinking=None, out=None, guard_repo=None,
         isolation=None, extra=None) -> list:
    cmd = [sys.executable, str(TRACE_RUN), str(cwd), prompt, "--adapter", adapter]
    if isolation:
        cmd += ["--isolation", str(isolation)]
    if no_skill:
        cmd.append("--no-skill")
    elif skill_dir:
        cmd += ["--skill", str(skill_dir)]
    if early_exit:
        cmd.append("--early-exit")
    if model:
        provider = _config.get_str("harness.provider", "").strip()
        if provider and "/" not in model:
            cmd += ["--provider", provider]
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
             guard_repo=None, timeout=None, build_only=False, adapter=None,
             isolation=None) -> dict:
    """跑一次 case，返回 trace dict；build_only 返回 {"command": [...]}。

    isolation=None 保持旧调用：trace_run 默认 host；runner 在 T2/T3 显式传 docker。
    """
    name = current(adapter)
    cmd = _cmd(name, prompt, cwd, skill_dir=skill_dir, no_skill=no_skill,
               early_exit=early_exit, model=model, thinking=thinking, out=out,
               guard_repo=guard_repo, isolation=isolation,
               extra=["--build-only"] if build_only else None)
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
