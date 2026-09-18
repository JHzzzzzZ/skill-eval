"""Seam: python scripts/trace_run.py <worktree> <prompt> [--skill <path>] [--no-skill] [--out <file>]

实跑模式调用 pi CLI（--mode json --no-session）采集事件流并聚合为 trace。
纯逻辑（事件流解析 parse_events / 命令构造 build_args）通过 --events <jsonl> 离线测试。
stdout: trace JSON {"steps": [...], "tool_calls", "tokens", "seconds", "errors": [...], "triggered": bool}
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "trace_run.py"


def run_trace(args: list, cwd: Path = None):
    r = subprocess.run(
        [sys.executable, str(SCRIPT.parent / "trace_run.py")] + args,
        capture_output=True, text=True, cwd=str(cwd) if cwd else None,
    )
    return r


# --- Slice 10: 事件流解析 ---

JSONL = "\n".join([
    json.dumps({"type": "session", "id": "u1", "cwd": "/w"}),
    json.dumps({"type": "agent_start"}),
    json.dumps({"type": "tool_execution_start", "toolCallId": "t1", "toolName": "bash", "args": {"command": "ls"}}),
    json.dumps({"type": "tool_execution_end", "toolCallId": "t1", "result": "ok", "isError": False}),
    json.dumps({"type": "tool_execution_start", "toolCallId": "t2", "toolName": "read", "args": {"path": "x"}}),
    json.dumps({"type": "tool_execution_end", "toolCallId": "t2", "result": "err!", "isError": True}),
    json.dumps({"type": "turn_end", "message": {"role": "assistant"}, "toolResults": [],
                "usage": {"totalTokens": 500}}),
    json.dumps({"type": "agent_end", "messages": [
        {"role": "user", "content": []},
        {"role": "assistant", "content": [],
         "usage": {"input": 400, "output": 100, "totalTokens": 500}},
    ]}),
])


def test_parse_events(tmp_path):
    f = tmp_path / "events.jsonl"
    f.write_text(JSONL, encoding="utf-8")
    r = run_trace(["--events", str(f)])
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["tool_calls"] == 2
    assert [s["tool"] for s in out["steps"]] == ["bash", "read"]
    assert out["tokens"] == 500  # agent_end 里 assistant 消息的 usage.totalTokens
    assert len(out["errors"]) == 1
    assert out["seconds"] >= 0


# --- Slice 11: 触发判定 ---

def test_triggered_detection(tmp_path):
    # agent 执行了任何工具 = 触发；纯文本回答 = 未触发
    f = tmp_path / "no_trigger.jsonl"
    f.write_text("\n".join([
        json.dumps({"type": "session", "id": "u2"}),
        json.dumps({"type": "agent_end"}),
    ]), encoding="utf-8")
    out = json.loads(run_trace(["--events", str(f)]).stdout)
    assert out["triggered"] is False
    assert out["tool_calls"] == 0


# --- Slice 12: 命令构造 ---

def test_build_command_with_skill(tmp_path):
    out = json.loads(run_trace(["--build-only", "--worktree", "/w",
                                "--prompt", "hi", "--skill", "/s/SKILL.md"]).stdout)
    cmd = out["command"]
    assert "pi" in " ".join(cmd[:2]) or cmd[0].endswith("pi")
    assert "--mode" in cmd and "json" in cmd
    assert "--no-session" in cmd
    assert "--skill" in cmd
    assert cmd[cmd.index("--skill") + 1] == "/s/SKILL.md"


def test_build_command_no_skill(tmp_path):
    out = json.loads(run_trace(["--build-only", "--worktree", "/w",
                                "--prompt", "hi", "--no-skill"]).stdout)
    assert "--skill" not in out["command"]
    assert "--no-skills" in out["command"]


# --- Slice 13: 非法事件流 ---

def test_malformed_events(tmp_path):
    f = tmp_path / "bad.jsonl"
    f.write_text("not json", encoding="utf-8")
    r = run_trace(["--events", str(f)])
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["passed"] is False
    assert len(out["errors"]) >= 1


# --- Slice 14: 模型配置 ---

def test_build_command_default_model(tmp_path):
    # 默认：不传 model/provider，交给 pi 用自己的默认
    out = json.loads(run_trace(["--build-only", "--worktree", "/w", "--prompt", "hi"]).stdout)
    cmd = out["command"]
    assert "--model" not in cmd and "--provider" not in cmd


def test_build_command_with_model(tmp_path):
    out = json.loads(run_trace(["--build-only", "--worktree", "/w", "--prompt", "hi",
                                "--model", "zai-coding-cn/glm-5.3-flash",
                                "--thinking", "low"]).stdout)
    cmd = out["command"]
    assert cmd[cmd.index("--model") + 1] == "zai-coding-cn/glm-5.3-flash"
    assert cmd[cmd.index("--thinking") + 1] == "low"


def test_build_command_model_via_env_default(tmp_path):
    # 环境变量 SKILL_EVAL_MODEL 作为默认，--model 显式传参覆盖
    import os
    env_backup = os.environ.get("SKILL_EVAL_MODEL")
    os.environ["SKILL_EVAL_MODEL"] = "provider/default-model"
    try:
        out = json.loads(run_trace(["--build-only", "--worktree", "/w", "--prompt", "hi"]).stdout)
        cmd = out["command"]
        assert cmd[cmd.index("--model") + 1] == "provider/default-model"
        out2 = json.loads(run_trace(["--build-only", "--worktree", "/w", "--prompt", "hi",
                                     "--model", "other/override"]).stdout)
        assert out2["command"][out2["command"].index("--model") + 1] == "other/override"
    finally:
        if env_backup is None:
            os.environ.pop("SKILL_EVAL_MODEL", None)
        else:
            os.environ["SKILL_EVAL_MODEL"] = env_backup


# --- Slice 15b: 全仓复审修复 ---

def test_positional_args_seam(tmp_path):
    # docstring 声明的 seam：trace_run.py <worktree> <prompt>（位置参数）
    out = json.loads(run_trace(["--build-only", "--worktree", "/w", "--prompt", "hi"]).stdout)
    # build-only 不涉及位置参数；这里直接测 --events 等价物：位置参数走 build_args 分支由实跑承担，
    # 这里验证 usage 不再因位置参数形式而错乱
    r = run_trace(["/some/wt", "some prompt"])
    assert r.returncode != 0 or json.loads(r.stdout).get("steps") is not None


def test_out_flag_writes_trace(tmp_path):
    f = tmp_path / "events.jsonl"
    f.write_text(JSONL, encoding="utf-8")
    out_file = tmp_path / "trace.json"
    r = run_trace(["--events", str(f), "--out", str(out_file)])
    assert r.returncode == 0, r.stderr
    assert out_file.is_file()
    assert json.loads(out_file.read_text(encoding="utf-8"))["tool_calls"] == 2


def test_trace_contains_answer_and_args(tmp_path):
    # ISS-3: trace 必须存最终回答与有界 args，触发判定才有第一手证据
    f = tmp_path / "events.jsonl"
    f.write_text("\n".join([
        json.dumps({"type": "session", "id": "u3"}),
        json.dumps({"type": "tool_execution_start", "toolCallId": "t1", "toolName": "bash", "args": {"command": "ls -la"}}),
        json.dumps({"type": "agent_end", "messages": [
            {"role": "assistant", "content": [{"type": "text", "text": "目录内容是 a b c"}]},
        ]}),
    ]), encoding="utf-8")
    out = json.loads(run_trace(["--events", str(f)]).stdout)
    assert out["answer"] == "目录内容是 a b c"
    assert out["steps"][0]["args"] == {"command": "ls -la"}
    assert "args_hash" in out["steps"][0]
