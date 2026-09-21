"""evalset_check.py 断链修复前先立契约：条数闸门的计数口径、配置优先级、坏文件不计。

Seam: python scripts/evalset_check.py <evalsets/<name>/vN> [--out <file>] [--min N]
[--min-should N] [--min-not N] [--min-confusable N] -> stdout JSON
{"counts": {"should", "should_not", "confusable"}, "minimums", "passed", "issues"}
三组触发集各 ≥ 最小条数（默认 10）才 passed；cases 不在本闸门范围。
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "evalset_check.py"


def run_check(ev: Path, *args, env=None):
    import os
    e = dict(os.environ)
    e.pop("SKILL_EVAL_TRIGGER_MIN", None)
    if env:
        e.update(env)
    r = subprocess.run([sys.executable, str(SCRIPT), str(ev), *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=e)
    assert r.returncode == 0, f"evalset_check.py failed: {r.stderr}"
    return json.loads(r.stdout)


def make_evalset(tmp_path: Path, counts: dict):
    ev = tmp_path / "evalsets" / "s" / "v1"
    for sub, n in counts.items():
        d = ev / "triggers" / sub
        d.mkdir(parents=True, exist_ok=True)
        for i in range(1, n + 1):
            (d / f"{i}.json").write_text(json.dumps({"prompt": f"p{i}"}), encoding="utf-8")
    return ev


def test_below_default_min_fails(tmp_path):
    # 默认最小 10：6/3/4 全不达标（现状 todo-add v1 就是这个形状）
    ev = make_evalset(tmp_path, {"should": 6, "should-not": 3, "confusable": 4})
    out = run_check(ev)
    assert not out["passed"]
    assert out["minimums"] == {"should": 10, "should_not": 10, "confusable": 10}
    assert len(out["issues"]) == 3


def test_meeting_default_min_passes(tmp_path):
    ev = make_evalset(tmp_path, {"should": 10, "should-not": 10, "confusable": 10})
    out = run_check(ev)
    assert out["passed"] and not out["issues"]
    assert out["counts"] == {"should": 10, "should_not": 10, "confusable": 10}


def test_env_override(tmp_path):
    # 环境变量 SKILL_EVAL_TRIGGER_MIN 抬高/降低三组默认值
    ev = make_evalset(tmp_path, {"should": 6, "should-not": 6, "confusable": 6})
    out = run_check(ev, env={"SKILL_EVAL_TRIGGER_MIN": "6"})
    assert out["passed"] and out["minimums"] == {"should": 6, "should_not": 6, "confusable": 6}


def test_cli_overrides_env_and_per_group(tmp_path):
    ev = make_evalset(tmp_path, {"should": 5, "should-not": 3, "confusable": 5})
    # --min 全局 5，但 --min-not 单独收紧到 3
    out = run_check(ev, "--min", "5", "--min-not", "3",
                    env={"SKILL_EVAL_TRIGGER_MIN": "99"})
    assert out["passed"]
    assert out["minimums"] == {"should": 5, "should_not": 3, "confusable": 5}


def test_bad_json_not_counted_but_flagged(tmp_path):
    ev = make_evalset(tmp_path, {"should": 10, "should-not": 10, "confusable": 10})
    bad = ev / "triggers" / "should" / "x.json"
    bad.write_text("{oops", encoding="utf-8")
    empty = ev / "triggers" / "should" / "y.json"
    empty.write_text(json.dumps({"prompt": "  "}), encoding="utf-8")
    out = run_check(ev)
    assert out["counts"]["should"] == 10  # 坏文件不计数
    assert not out["passed"]              # 但记入 issues 拦下
    assert any("x.json" in s for s in out["issues"]) and any("y.json" in s for s in out["issues"])


def test_missing_dir_counts_zero(tmp_path):
    ev = make_evalset(tmp_path, {"should": 10, "should-not": 10})
    out = run_check(ev)
    assert out["counts"]["confusable"] == 0 and not out["passed"]


def test_out_file_utf8(tmp_path):
    ev = make_evalset(tmp_path, {"should": 10, "should-not": 10, "confusable": 10})
    dst = tmp_path / "chk.json"
    run_check(ev, "--out", str(dst))
    assert dst.read_text(encoding="utf-8")  # --out 落盘 UTF-8，无 GBK 风险


# --- meta.counts 新鲜度：冻结后扩条未回写 → issue 提示（不阻断闸门） ---

def _mk_entry(d, name):
    (d / name).write_text(json.dumps({"prompt": "p"}), encoding="utf-8")


def test_meta_counts_stale_reported(tmp_path):
    import subprocess, sys as _sys
    script = Path(__file__).parent.parent / "scripts" / "evalset_check.py"
    for g in ("should", "should-not", "confusable"):
        (tmp_path / "triggers" / g).mkdir(parents=True)
        for i in range(10):
            _mk_entry(tmp_path / "triggers" / g, f"{i}.json")
    (tmp_path / "meta.json").write_text(json.dumps(
        {"counts": {"should": 6, "should_not": 10, "confusable": 10}}), encoding="utf-8")
    r = subprocess.run([_sys.executable, str(script), str(tmp_path)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = json.loads(r.stdout)
    assert out["passed"] is True  # 条数闸门本身达标，新鲜度不阻断
    assert any("counts[should]=6 与实际 10 不符" in w for w in out["warnings"])
