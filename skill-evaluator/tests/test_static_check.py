"""Seam: python scripts/static_check.py <skill目录> -> stdout JSON

契约（reference.md § 指标映射）：
- exit code 0 = 检查完成（不代表 skill 合格）
- stdout = JSON，含 name/description/调用方式/权限扫描结果
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "static_check.py"


def run_check(skill_dir: Path) -> dict:
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(skill_dir)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"script failed: {r.stderr}"
    return json.loads(r.stdout)


def make_skill(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "sample-skill"
    d.mkdir()
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    return d


# --- Slice 1: frontmatter 解析 ---

def test_valid_frontmatter(tmp_path):
    d = make_skill(tmp_path, (
        "---\n"
        "name: sample-skill\n"
        'description: "Does a thing. Use when asked to do a thing."\n'
        "---\n"
        "# Body\n"
    ))
    out = run_check(d)
    assert out["name"] == "sample-skill"
    assert "thing" in out["description"]
    assert out["passed"] is True


def test_missing_name(tmp_path):
    d = make_skill(tmp_path, (
        "---\n"
        'description: "Something."\n'
        "---\n"
        "body\n"
    ))
    out = run_check(d)
    assert out["passed"] is False
    assert any("name" in issue for issue in out["issues"])


# --- Slice 2: description token 超限告警（#2）---

def test_description_over_token_limit(tmp_path):
    # 100 token 阈值：400+ 英文字符即超
    long_desc = "word " * 100
    d = make_skill(tmp_path, (
        "---\n"
        "name: sample-skill\n"
        f'description: "{long_desc}"\n'
        "---\n"
        "body\n"
    ))
    out = run_check(d)
    assert any("token" in issue for issue in out["issues"])


def test_description_within_limit_no_warning(tmp_path):
    d = make_skill(tmp_path, (
        "---\n"
        "name: sample-skill\n"
        'description: "Short description."\n'
        "---\n"
        "body\n"
    ))
    out = run_check(d)
    assert not any("token" in issue for issue in out["issues"])


# --- Slice 3: 危险命令扫描（#13）---

def test_dangerous_command_in_script(tmp_path):
    d = make_skill(tmp_path, "---\nname: s\ndescription: d\n---\nbody\n")
    (d / "do.sh").write_text("#!/bin/sh\nrm -rf $HOME\n", encoding="utf-8")
    out = run_check(d)
    assert len(out["dangerous"]) == 1
    assert out["dangerous"][0]["file"] == "do.sh"
    assert any("危险" in issue for issue in out["issues"])


def test_clean_skill_no_dangerous(tmp_path):
    d = make_skill(tmp_path, "---\nname: s\ndescription: d\n---\nbody\n")
    out = run_check(d)
    assert out["dangerous"] == []


# --- Slice 3b: 失败路径与 #7（P1-2/P1-3/P2-5/6）---

def test_dangerous_hits_fail_static_check(tmp_path):
    # #13 命中 → errors 非空且 passed=False（三档结论的"失败"档）
    d = make_skill(tmp_path, "---\nname: s\ndescription: d\n---\nbody\n")
    (d / "evil.sh").write_text("rm -rf /\n", encoding="utf-8")
    out = run_check(d)
    assert out["passed"] is False
    assert len(out["errors"]) >= 1


def test_token_overlimit_is_warning_not_error(tmp_path):
    long_desc = "word " * 100
    d = make_skill(tmp_path, (
        "---\nname: s\n"
        f'description: "{long_desc}"\n---\nbody\n'
    ))
    out = run_check(d)
    # token 超限是"警告"档，不算失败
    assert out["passed"] is True
    assert len(out["warnings"]) == 1


def test_invoke_field_validated(tmp_path):
    # #7: frontmatter 有 invoke 且合法 → 回显解析结果（规范值 human/agent/both）
    d = make_skill(tmp_path, (
        "---\nname: s\ndescription: d\ninvoke: agent\n---\nbody\n"
    ))
    out = run_check(d)
    assert out["invoke"]["resolved"] == "agent"


def test_invoke_field_invalid(tmp_path):
    d = make_skill(tmp_path, (
        "---\nname: s\ndescription: d\ninvoke: whatever\n---\nbody\n"
    ))
    out = run_check(d)
    assert out["passed"] is False
    assert any("invoke" in e for e in out["errors"])


def test_invoke_missing_defaults_both(tmp_path):
    d = make_skill(tmp_path, "---\nname: s\ndescription: d\n---\nbody\n")
    out = run_check(d)
    assert out["invoke"]["resolved"] == "both"


def test_missing_argv_usage_not_crash():
    r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode != 0
    assert "usage" in (r.stdout + r.stderr).lower()


def test_non_utf8_skill_md_not_crash(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / "SKILL.md").write_bytes(b"---\nname: s\ndescription: \xff\xfe\n---\nbody\n")
    out = run_check(d)
    assert out["passed"] is False  # frontmatter 解析不出 name


# --- 自检测假阳性修复：--exclude ---

# --- 自检测假阳性修复：--exclude ---

def test_exclude_file(tmp_path):
    d = make_skill(tmp_path, "---\nname: s\ndescription: d\n---\nbody\n")
    (d / "tool.sh").write_text("rm -rf /tmp/x\n", encoding="utf-8")
    out = run_check(d)
    assert len(out["dangerous"]) == 1  # 默认仍扫出
    out2 = run_check([str(d), "--exclude", str(d / "tool.sh")])
    assert out2["dangerous"] == []     # 排除后不再误报


def test_exclude_directory(tmp_path):
    d = make_skill(tmp_path, "---\nname: s\ndescription: d\n---\nbody\n")
    (d / "tests").mkdir()
    (d / "tests" / "fixtures.py").write_text('x = "rm -rf /tmp"\n', encoding="utf-8")
    out = run_check([str(d), "--exclude", str(d / "tests")])
    assert out["dangerous"] == []
