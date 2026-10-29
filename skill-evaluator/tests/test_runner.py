"""runner.py 的 dry-run / T0 端到端（不调用 pi / LLM）。"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "runner.py"


def make_skill(tmp_path: Path) -> Path:
    d = tmp_path / "sample"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: sample\ndescription: d\n---\nbody\n", encoding="utf-8")
    return d


def test_dry_run_plan(tmp_path):
    skill = make_skill(tmp_path)
    r = subprocess.run([sys.executable, str(SCRIPT), "--skill", str(skill),
                        "--tier", "t2b", "--dry-run"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    plan = json.loads(r.stdout.splitlines()[0])
    assert plan["type"] == "plan"
    assert plan["stages"] == ["t0", "t1", "t2a", "t2b"]


def test_t0_end_to_end_writes_schema2(tmp_path):
    skill = make_skill(tmp_path)
    results = tmp_path / "results"
    r = subprocess.run([sys.executable, str(SCRIPT), "--skill", str(skill),
                        "--tier", "t0", "--results", str(results)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    report = json.loads((results / "report.json").read_text(encoding="utf-8"))
    assert report["schema"] == 2
    assert len(report["metrics"]) == 16


def test_missing_skill_is_error(tmp_path):
    r = subprocess.run([sys.executable, str(SCRIPT), "--skill", str(tmp_path),
                        "--tier", "t0", "--dry-run"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 2
