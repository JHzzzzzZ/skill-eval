"""pairwise.py 的离线 prompt 契约（不调用 LLM）。"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "pairwise.py"


def test_pairwise_build_prompt_anonymizes_sides(tmp_path):
    prompt = tmp_path / "prompt.txt"
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    prompt.write_text("修这个 bug", encoding="utf-8")
    a.write_text("回答一", encoding="utf-8")
    b.write_text("回答二", encoding="utf-8")
    r = subprocess.run([sys.executable, str(SCRIPT), "--prompt", str(prompt),
                        "--a", str(a), "--b", str(b), "--build-only"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "回答 A" in out["prompt"] and "回答 B" in out["prompt"]
    assert "回答一" in out["prompt"] and "回答二" in out["prompt"]
    assert "with_skill" not in out["prompt"]
