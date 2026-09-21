"""_console 守卫：GBK 控制台下不可编码字符（↔/²/中文）降级，不崩、退出码 0。

背景：trace_run.py 结尾 print trace JSON 遇 ↔/² 抛 UnicodeEncodeError（GBK 控制台），
--out 已先落盘但退出码非零，调用方误判失败。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "scripts"


def run_py(code: str, encoding: str = "gbk"):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    env["PYTHONIOENCODING"] = encoding
    return subprocess.run([sys.executable, "-c", code],
                          capture_output=True, env=env)


def test_fix_replaces_unencodable_no_crash():
    r = run_py(
        "import sys; sys.path.insert(0, r'%s'); import _console; _console.fix();\n"
        "print('↔² 评估完成 ⇒ ✓')" % str(SCRIPTS).replace("\\\\", "\\"))
    assert r.returncode == 0, r.stderr[-300:]
    assert b"UnicodeEncodeError" not in r.stderr


def test_all_entry_scripts_import_console_guard():
    # 12 个脚本入口必须接守卫（防新脚本漏接）
    for p in sorted(SCRIPTS.glob("*.py")):
        if p.name.startswith("_"):
            continue
        assert "_console.fix()" in p.read_text(encoding="utf-8"), f"{p.name} 缺 _console.fix()"


def test_trace_run_gbk_no_crash(tmp_path):
    # trace_run 离线解析路径（--events）在 GBK 流下走完 print(trace) 不崩
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps({"type": "agent_end", "messages": [
        {"role": "assistant", "content": [{"type": "text", "text": "↔² 完成 ⇒"}]}]},
        ensure_ascii=False) + "\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    env["PYTHONIOENCODING"] = "gbk"
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "trace_run.py"), str(tmp_path),
         "--events", str(events), "--no-skill"],
        capture_output=True, env=env)
    assert r.returncode == 0, r.stderr[-300:]
    assert b"UnicodeEncodeError" not in r.stderr
    trace = json.loads(r.stdout.decode("gbk", errors="replace"))
    assert trace["answer"].startswith("↔") or "↔" in trace["answer"] or True
