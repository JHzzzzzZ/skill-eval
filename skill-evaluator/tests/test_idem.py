"""Seam: python scripts/idem.py <trace1.json> <trace2.json> -> stdout JSON

trace 格式: {"steps": [{"tool": str, "args_hash": str}, ...]}
幂等判定（#15）: 第二次运行重复做了第一次已完成的步骤 → ratio 高 = 不幂等。
stdout: {"total_steps", "repeated_steps", "ratio", "idempotent"}
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "idem.py"


def write_trace(p: Path, steps: list) -> Path:
    p.write_text(json.dumps({"steps": steps}), encoding="utf-8")
    return p


def run_idem(t1: Path, t2: Path) -> dict:
    r = subprocess.run(
        [sys.executable, str(SCRIPT.parent / "idem.py"), str(t1), str(t2)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"script failed: {r.stderr}"
    return json.loads(r.stdout)


# --- Slice 6: 幂等比例 ---

def test_fully_repeated_not_idempotent(tmp_path):
    steps = [{"tool": "bash", "args_hash": "a1"}, {"tool": "read", "args_hash": "b2"}]
    t1 = write_trace(tmp_path / "t1.json", steps)
    t2 = write_trace(tmp_path / "t2.json", steps)  # 第二次原样重复 = 不幂等
    out = run_idem(t1, t2)
    assert out["repeated_steps"] == 2
    assert out["ratio"] == 1.0
    assert out["idempotent"] is False


def test_disjoint_steps_idempotent(tmp_path):
    t1 = write_trace(tmp_path / "t1.json", [{"tool": "bash", "args_hash": "a1"}])
    t2 = write_trace(tmp_path / "t2.json", [{"tool": "read", "args_hash": "z9"}])
    out = run_idem(t1, t2)
    assert out["repeated_steps"] == 0
    assert out["idempotent"] is True


def test_step_counts_from_second_run(tmp_path):
    # total_steps 以第二次运行为准
    t1 = write_trace(tmp_path / "t1.json", [{"tool": "bash", "args_hash": "a1"}])
    t2 = write_trace(tmp_path / "t2.json", [
        {"tool": "bash", "args_hash": "a1"},
        {"tool": "read", "args_hash": "z9"},
    ])
    out = run_idem(t1, t2)
    assert out["total_steps"] == 2
    assert out["repeated_steps"] == 1
    assert out["ratio"] == 0.5
    assert out["idempotent"] is True  # 阈值默认 0.5


# --- Slice 7: 非法输入 ---

def test_malformed_trace(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"wrong_field": []}', encoding="utf-8")
    t1 = write_trace(tmp_path / "t1.json", [{"tool": "bash", "args_hash": "a"}])
    r = subprocess.run(
        [sys.executable, str(SCRIPT.parent / "idem.py"), str(t1), str(bad)],
        capture_output=True, text=True,
    )
    assert r.returncode != 0, "非法 trace 应报错退出而非静默给 0"
