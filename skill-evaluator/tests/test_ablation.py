"""Seam: python scripts/ablation.py --skill <skill目录> --delete "标题1,标题2" --out <输出目录>

#6 消融运行的数据准备：把 SKILL.md 按 ## 标题切段，删掉指定段，连同其余文件复制到输出目录。
--list 只列出段落名。输出: {"sections": [...], "deleted": [...], "out": <路径>}
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "ablation.py"

SKILL_BODY = (
    "---\nname: s\ndescription: d\n---\n"
    "# Title\nintro text\n"
    "## 概念说明\nThis is a long explanation of concept A that the agent already knows.\n"
    "## 步骤\n1. do a\n2. do b\n"
    "## 索引\nSee reference.md for details.\n"
)


def make_skill(tmp_path: Path) -> Path:
    d = tmp_path / "skill"
    d.mkdir()
    (d / "SKILL.md").write_text(SKILL_BODY, encoding="utf-8")
    (d / "reference.md").write_text("details here", encoding="utf-8")
    return d


def run_ablation(args: list):
    return subprocess.run(
        [sys.executable, str(SCRIPT.parent / "ablation.py")] + args,
        capture_output=True, text=True,
    )


# --- Slice 24: 段落列举 ---

def test_list_sections(tmp_path):
    d = make_skill(tmp_path)
    r = run_ablation(["--skill", str(d), "--list"])
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "## 概念说明" in out["sections"]
    assert "## 步骤" in out["sections"]


# --- Slice 25: 段落删除 ---

def test_delete_section(tmp_path):
    d = make_skill(tmp_path)
    out_dir = tmp_path / "ablated"
    r = run_ablation(["--skill", str(d), "--delete", "概念说明", "--out", str(out_dir)])
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "概念说明" in out["deleted"]
    new_text = (out_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "concept A" not in new_text      # 段落正文被删
    assert "## 步骤" in new_text            # 其他段落保留
    assert (out_dir / "reference.md").read_text(encoding="utf-8") == "details here"  # 附属文件原样复制


def test_delete_unknown_section_fails(tmp_path):
    d = make_skill(tmp_path)
    r = run_ablation(["--skill", str(d), "--delete", "不存在的段落", "--out", str(tmp_path / "o")])
    assert r.returncode != 0


# --- 全仓复审 P2: 子目录复制 + 缺目录守卫 ---

def test_copies_subdirectories(tmp_path):
    d = make_skill(tmp_path)
    (d / "assets").mkdir()
    (d / "scripts").mkdir()
    (d / "scripts" / "tool.py").write_text("print(1)", encoding="utf-8")
    out_dir = tmp_path / "ablated"
    r = run_ablation(["--skill", str(d), "--delete", "概念说明", "--out", str(out_dir)])
    assert r.returncode == 0, r.stderr
    assert (out_dir / "scripts" / "tool.py").is_file()
    assert (out_dir / "SKILL.md").is_file()


def test_missing_skill_dir_controlled(tmp_path):
    r = run_ablation(["--skill", str(tmp_path / "nope"), "--list"])
    assert r.returncode != 0
    assert "traceback" not in r.stderr.lower()
