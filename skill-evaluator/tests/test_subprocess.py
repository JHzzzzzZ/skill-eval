"""Seam: scripts/_subprocess.py —— 三判定脚本共享的 pi 执行壳。

回归锁定两个实测缺陷（本机 Windows）：
1. 判定调用必须走 --mode json（compare.extract_answer 只认事件流；text 模式恒解析失败）
2. prompt 必须走 stdin（长 prompt 走 argv 在 Windows 会 CreateProcess 失败，rc=1 空输出）
3. 超时必须杀整棵进程树（taskkill /T），杀壳杀不到 node 孙进程 → 管道永不 EOF
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import _subprocess  # noqa: E402
from _subprocess import build_mode_args, json_args, resolve_pi, run_cmd, run_pi  # noqa: E402


# --- 命令构造（纯逻辑） ---

def test_json_args_always_mode_json():
    # 缺陷修复回归：判定调用必须是 --mode json 事件流，不能是 text 模式 -p
    args = json_args(["--model", "p/m"])
    assert args[:4] == ["--mode", "json", "--no-session", "--no-skills"]
    assert "-p" not in args


def test_build_mode_args_with_model_and_thinking():
    args = build_mode_args(model="p/m", thinking="low")
    assert args[args.index("--model") + 1] == "p/m"
    assert args[args.index("--thinking") + 1] == "low"


def test_build_mode_args_without_model():
    # 不传 model 时不注入 --model（SKILL_EVAL_MODEL 兜底由调用方解析）
    assert build_mode_args(model=None, thinking=None) == []


# --- stdin 通路（真实子进程，但不用 pi：用 python 自身模拟"读 stdin 输出事件流"） ---

def test_run_cmd_passes_prompt_via_stdin(tmp_path):
    # prompt 走 stdin：模拟 pi 行为的子进程读 stdin 并回显为事件流
    import json as _json
    reader = tmp_path / "reader.py"
    reader.write_text(
        "import sys, json\n"
        "text = sys.stdin.read()\n"
        "print(json.dumps({'type': 'agent_end', 'messages': ["
        "{'role': 'assistant', 'content': [{'type': 'text', 'text': text[:40]}]}]}, ensure_ascii=False))\n",
        encoding="utf-8")
    long_prompt = "长" * 5000  # 1 万字符：argv 通路在这个量级会 CreateProcess 失败
    rc, out, err = run_cmd([sys.executable, str(reader)], prompt=long_prompt, timeout=60)
    assert rc == 0, err
    assert long_prompt[:40] in out  # 完整收到了 stdin 里的 prompt


def test_run_pi_timeout_kills_process_tree(tmp_path):
    # 超时：杀树后必须返回（重抛 TimeoutExpired），绝不能悬挂
    import pytest
    import time
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text("import time; time.sleep(60)\n", encoding="utf-8")
    t0 = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        run_cmd([sys.executable, str(sleeper)], prompt="hi", timeout=2)
    assert time.time() - t0 < 20  # 秒级返回，而不是等 sleep(60)


def test_run_pi_missing_pi_raises_filenotfound(monkeypatch):
    # pi 不存在 → FileNotFoundError（调用方转保守裁决，不静默通过）
    monkeypatch.setattr(_subprocess, "resolve_pi", lambda: None)
    import pytest
    with pytest.raises(FileNotFoundError):
        run_pi(["--mode", "json"], prompt="hi")


def test_resolve_pi_finds_pi():
    # 本机装了 pi（npm shim），resolve 必须命中且是绝对路径
    p = resolve_pi()
    assert p is not None and Path(p).is_absolute()
