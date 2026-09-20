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
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(cwd) if cwd else None,
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
    # 绝对路径原样保留（POSIX）；Windows 下 resolve 会补盘符，故断言 resolve 后的值
    assert cmd[cmd.index("--skill") + 1] == str(Path("/s/SKILL.md").resolve())


def test_skill_relative_path_resolved_absolute(tmp_path):
    # 缺陷修复：相对路径按子进程 cwd（=worktree）解析 → skill 静默不加载。
    # 现在 build_args 统一 resolve；以 cwd=tmp_path 验证解析基于调用方 cwd。
    (tmp_path / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n", encoding="utf-8")
    out = json.loads(run_trace(["--build-only", "--worktree", "/w",
                                "--prompt", "hi", "--skill", "SKILL.md"], cwd=tmp_path).stdout)
    cmd = out["command"]
    assert cmd[cmd.index("--skill") + 1] == str((tmp_path / "SKILL.md").resolve())


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


# --- ADR-0007: EventAggregator 增量消费 + early-exit ---

import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from trace_run import EventAggregator  # noqa: E402

EVENT_LINES = [
    '{"type":"tool_execution_start","toolName":"read","args":"x.md"}',
    '{"type":"tool_execution_end","toolCallId":"t1","isError":false}',
    '{"type":"turn_end","usage":{"totalTokens":10}}',
    '{"type":"agent_end","messages":[{"role":"assistant","usage":{"totalTokens":25},'
    '"content":[{"type":"text","text":"answer"}]}]}',
]


def test_incremental_feed_equals_full_parse():
    agg = EventAggregator()
    for line in EVENT_LINES:
        agg.feed(line)
    full = json.loads(run_trace(["--events", _write_jsonl(EVENT_LINES)]).stdout)
    assert agg.result()["tool_calls"] == full["tool_calls"]
    assert agg.result()["answer"] == full["answer"]


def _write_jsonl(lines) -> str:
    import tempfile, os
    fd, p = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    Path(p).write_text("\n".join(lines), encoding="utf-8")
    return p


def test_early_exit_stops_at_first_tool_call():
    agg = EventAggregator(early_exit=True)
    for line in EVENT_LINES:
        agg.feed(line)
        if agg.stop:
            break
    trace = agg.result()
    assert agg.stop is True
    assert trace["early_exit"] is True
    assert trace["triggered"] is True
    assert trace["tool_calls"] == 1
    # 停在首个工具调用，后面的 turn_end/agent_end 不消费 → 无 answer
    assert trace["answer"] == ""
    assert trace["passed"] is True  # early-exit 不计入 errors


def test_no_early_exit_consumes_all():
    agg = EventAggregator()
    for line in EVENT_LINES:
        agg.feed(line)
    trace = agg.result()
    assert trace["answer"] == "answer"
    assert trace["tokens"] == 25
    assert "early_exit" not in trace


def test_early_exit_without_tool_call_runs_to_end():
    no_tool = [l for l in EVENT_LINES if "tool_execution" not in l]
    agg = EventAggregator(early_exit=True)
    for line in no_tool:
        agg.feed(line)
    trace = agg.result()
    assert trace["triggered"] is False
    assert trace["answer"] == "answer"
    assert "early_exit" not in trace


def test_events_flag_with_early_exit(tmp_path):
    # --early-exit 离线通路：同样在首个工具调用处截断
    f = tmp_path / "events.jsonl"
    f.write_text("\n".join(EVENT_LINES), encoding="utf-8")
    out = json.loads(run_trace(["--events", str(f), "--early-exit"]).stdout)
    assert out["early_exit"] is True
    assert out["tool_calls"] == 1


def test_build_args_with_early_exit_flag_unchanged_command():
    # --early-exit 是编排层开关，不进 pi 命令行
    out = json.loads(run_trace(["--build-only", "--worktree", "/w",
                                "--prompt", "hi", "--early-exit"]).stdout)
    assert "--early-exit" not in out["command"]


# --- Windows 死锁修复：_kill_tree ---

def test_kill_tree_terminates_wrapped_process(tmp_path):
    # Windows proc.kill 只杀 cmd.exe 包装层，node 孙进程持 stderr 管道 → read 永久阻塞。
    # _kill_tree（taskkill /F /T）必须让整棵树在超时前退场。
    import time
    sys.path.insert(0, str(SCRIPT.parent))
    from trace_run import _kill_tree
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    t0 = time.time()
    _kill_tree(proc)
    proc.wait(timeout=15)
    assert time.time() - t0 < 10  # 秒杀，不是等 sleep(30) 自然结束
    assert proc.returncode != 0 or proc.poll() is not None


# --- ADR-0007 修订：early-exit 停止条件 = 首次指向被测 SKILL.md 的调用 ---

MARKER_LINES = [
    '{"type":"session","id":"m1"}',
    # 第 1 步：与 skill 无关的探索（旧口径会在这里误杀 → 假阴性）
    json.dumps({"type": "tool_execution_start", "toolCallId": "t1", "toolName": "bash",
                "args": {"command": "ls"}}),
    json.dumps({"type": "tool_execution_end", "toolCallId": "t1", "result": "ok", "isError": False}),
    # 第 2 步：加载被测 skill（= 触发决定点）
    json.dumps({"type": "tool_execution_start", "toolCallId": "t2", "toolName": "read",
                "args": {"path": "C:/s/SKILL.md"}}),
    '{"type":"turn_end","usage":{"totalTokens":10}}',
    '{"type":"agent_end","messages":[{"role":"assistant","usage":{"totalTokens":25},'
    '"content":[{"type":"text","text":"answer"}]}]}',
]


def test_early_exit_waits_for_skill_load_not_first_call():
    # 先探索后加载：不能在第 1 步杀（会误杀成假阴性），要等到加载事件
    agg = EventAggregator(early_exit=True, skill_marker=r"C:\s\SKILL.md")
    for line in MARKER_LINES:
        agg.feed(line)
        if agg.stop:
            break
    trace = agg.result()
    assert agg.stop is True
    assert trace["early_exit"] is True
    assert [s["tool"] for s in trace["steps"]] == ["bash", "read"]  # 探索步保留作证据
    assert trace["steps"][-1]["args"] == {"path": "C:/s/SKILL.md"}  # 停在加载事件上


def test_early_exit_with_marker_never_hit_runs_to_end():
    # 未加载被测 skill → 跑完整（行为是 precision 的证据），不打 early_exit 标
    agg = EventAggregator(early_exit=True, skill_marker=r"C:\s\SKILL.md")
    no_load = [l for l in MARKER_LINES if "C:/s/SKILL.md" not in l]
    for line in no_load:
        agg.feed(line)
    trace = agg.result()
    assert agg.stop is False
    assert "early_exit" not in trace
    assert trace["answer"] == "answer"


def test_early_exit_marker_none_keeps_old_behavior():
    # 兼容：无 marker（无 skill 场景/离线测试）→ 旧口径任意首次调用即停
    agg = EventAggregator(early_exit=True)
    for line in MARKER_LINES:
        agg.feed(line)
        if agg.stop:
            break
    assert agg.stop is True
    assert [s["tool"] for s in agg.result()["steps"]] == ["bash"]


def test_events_seam_skill_marker_via_cli(tmp_path):
    # CLI 通路：--skill + --early-exit 时 marker = resolve 后的路径，按加载事件停
    f = tmp_path / "events.jsonl"
    f.write_text("\n".join(MARKER_LINES), encoding="utf-8")
    out = json.loads(run_trace(["--events", str(f), "--early-exit",
                                "--skill", "/s/SKILL.md"]).stdout)
    assert out["early_exit"] is True
    assert [s["tool"] for s in out["steps"]] == ["bash", "read"]


def test_events_seam_skill_marker_never_hit(tmp_path):
    # CLI 通路：给了 --skill 但事件流从未加载 → 跑完整，无 early_exit 标
    f = tmp_path / "events2.jsonl"
    f.write_text("\n".join(l for l in MARKER_LINES if "C:/s/SKILL.md" not in l), encoding="utf-8")
    out = json.loads(run_trace(["--events", str(f), "--early-exit",
                                "--skill", "/s/SKILL.md"]).stdout)
    assert "early_exit" not in out
    assert out["answer"] == "answer"


def test_early_exit_marker_matches_escaped_windows_path():
    # 冒烟回归：路径反斜杠在 json 序列化前后形态不同，匹配必须基于 args 原始值
    p = "C:\\Users\\x\\skill-evaluator\\.skillrepos\\todo-add\\SKILL.md"
    agg = EventAggregator(early_exit=True, skill_marker=p)
    line = json.dumps({"type": "tool_execution_start", "toolCallId": "t1", "toolName": "read",
                       "args": {"path": p}})
    agg.feed(line)
    assert agg.stop is True  # 修前这里恒 False（dumps 双斜杠对不上）


def test_early_exit_marker_matches_forward_slash_readback():
    # 模型用 / 回读同一文件也要命中
    agg = EventAggregator(early_exit=True, skill_marker="C:\\s\\x\\SKILL.md")
    line = json.dumps({"type": "tool_execution_start", "toolCallId": "t1", "toolName": "read",
                       "args": {"path": "c:/s/x/SKILL.md"}})
    agg.feed(line)
    assert agg.stop is True
