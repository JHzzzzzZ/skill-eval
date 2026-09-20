"""Seam: python scripts/evolution.py <evalsets/<name>> [--out <file>]

#14 版本演进：扫描 results/*/report.json，输出跨版本的每指标 verdict 变化表。
stdout: {"versions": [...], "changes": [{"metric", "from", "to", "versions"}], "summary"}
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "evolution.py"


def make_result(d: Path, version: str, verdicts: dict):
    vdir = d / "results" / version
    vdir.mkdir(parents=True)
    metrics = {k: {"verdict": v, "method": "静态检查", "data": None, "note": ""}
               for k, v in verdicts.items()}
    (vdir / "report.json").write_text(
        json.dumps({"metrics": metrics, "conclusion": "with-warnings"}, ensure_ascii=False),
        encoding="utf-8")


def run_evo(d: Path):
    r = subprocess.run(
        [sys.executable, str(SCRIPT.parent / "evolution.py"), str(d)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# --- Slice 25: 版本对比 ---

def test_single_version(tmp_path):
    make_result(tmp_path, "v1", {"#2": "pass", "#13": "fail"})
    out = run_evo(tmp_path)
    assert out["versions"] == ["v1"]
    assert out["changes"] == []  # 只有一个版本无从对比


def test_two_versions_change_detected(tmp_path):
    make_result(tmp_path, "v1", {"#2": "pass", "#13": "fail"})
    make_result(tmp_path, "v2", {"#2": "pass", "#13": "pass"})
    out = run_evo(tmp_path)
    assert out["versions"] == ["v1", "v2"]
    changes = {(c["metric"], c["from"], c["to"]) for c in out["changes"]}
    assert ("#13", "fail", "pass") in changes


def test_version_sorting_semver(tmp_path):
    make_result(tmp_path, "v2", {"#2": "pass"})
    make_result(tmp_path, "v10", {"#2": "pass"})
    make_result(tmp_path, "v1", {"#2": "fail"})
    out = run_evo(tmp_path)
    assert out["versions"] == ["v1", "v2", "v10"]  # 数字排序不是字符串排序


def test_missing_report_dir(tmp_path):
    out = run_evo(tmp_path)
    assert out["versions"] == []
    assert out["changes"] == []


# --- 全仓复审 P2: --out 顺序歧义守卫 ---

def test_hash_versions_sorted_by_mtime(tmp_path):
    import os, time
    make_result(tmp_path, "6568868", {"#2": "pass"})  # v2 后创建
    time.sleep(0.05)
    make_result(tmp_path, "7606790", {"#2": "pass"})  # v1 反而更早？模拟：先建 v1 名字的目录但 mtime 更早
    out = run_evo(tmp_path)
    # hash 名不参与数字语义排序：按 mtime；这里 7606790 目录 mtime 更晚 → 排后面
    assert out["versions"] == ["6568868", "7606790"]


def test_semver_still_numeric(tmp_path):
    make_result(tmp_path, "v10", {"#2": "pass"})
    make_result(tmp_path, "v2", {"#2": "pass"})
    out = run_evo(tmp_path)
    assert out["versions"] == ["v2", "v10"]


def test_semver_minor_ordering_numeric(tmp_path):
    # 次要版本段也按数字排：v1.2 < v1.10（原实现只取首个数字，退化为字符串序）
    make_result(tmp_path, "v1.10", {"#2": "pass"})
    make_result(tmp_path, "v1.2", {"#2": "fail"})
    out = run_evo(tmp_path)
    assert out["versions"] == ["v1.2", "v1.10"]


def test_out_natural_order_also_works(tmp_path):
    make_result(tmp_path, "v1", {"#2": "pass"})
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path), "--out", str(tmp_path / "o.json")],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "o.json").is_file()


# --- 版本序用 report.json 的 generated_at，mtime 会被事后写文件污染 ---

def test_generated_at_ordering(tmp_path):
    import json as j
    make_result(tmp_path, "aaa", {"#2": "pass"})
    make_result(tmp_path, "bbb", {"#2": "fail"})
    # 反向时间戳：bbb 更早
    for name, ts in (("aaa", "2026-01-02"), ("bbb", "2026-01-01")):
        p = tmp_path / "results" / name / "report.json"
        r = json.loads(p.read_text(encoding="utf-8"))
        r["generated_at"] = ts
        p.write_text(json.dumps(r), encoding="utf-8")
    out = run_evo(tmp_path)
    assert out["versions"] == ["bbb", "aaa"]
