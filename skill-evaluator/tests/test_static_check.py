"""Seam: python scripts/static_check.py <skill目录> [--out <file>] -> stdout JSON

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


def test_invoke_maps_disable_model_invocation(tmp_path):
    # #7: 映射 pi 真实 frontmatter 字段 disable-model-invocation（true → resolved=human）
    d = make_skill(tmp_path, "---\nname: s\ndescription: d\ndisable-model-invocation: true\n---\nbody\n")
    out = run_check(d)
    assert out["invoke"]["resolved"] == "human"
    assert "disable-model-invocation=true" in out["invoke"]["note"]


def test_invoke_false_or_missing_is_both(tmp_path):
    # false 与缺省同义：模型按 description 触发，用户也可手动调用
    d = make_skill(tmp_path, "---\nname: s\ndescription: d\ndisable-model-invocation: false\n---\nbody\n")
    out = run_check(d)
    assert out["invoke"]["resolved"] == "both"


def test_invoke_missing_defaults_both(tmp_path):
    # 字段缺省 = pi 默认 model-invoked：resolved=both 且 note 说明来源
    d = make_skill(tmp_path, "---\nname: s\ndescription: d\n---\nbody\n")
    out = run_check(d)
    assert out["invoke"]["resolved"] == "both"
    assert "缺省" in out["invoke"]["note"]


def test_invoke_nonboolean_is_warning_not_error(tmp_path):
    # pi 对非布尔值按未知字段忽略：记警告不算失败（pi 行为对齐）
    d = make_skill(tmp_path, "---\nname: s\ndescription: d\ndisable-model-invocation: maybe\n---\nbody\n")
    out = run_check(d)
    assert out["passed"] is True
    assert any("disable-model-invocation" in w for w in out["warnings"])


def test_out_writes_utf8_file(tmp_path):
    # --out：脚本自写 UTF-8（Windows shell 重定向产 GBK 是历史事故的根因）
    d = make_skill(tmp_path, "---\nname: s\ndescription: 记需求登记\n---\nbody\n")
    out_file = tmp_path / "static.json"
    r = subprocess.run([sys.executable, str(SCRIPT), str(d), "--out", str(out_file)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["invoke"]["resolved"] == "both"
    payload = json.loads(out_file.read_text(encoding="utf-8"))  # 非 UTF-8 字节会在这里炸
    assert payload["description"] == "记需求登记"


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


# --- 可移植性闸门（ADR-0008）：宿主环境硬编码 → 整体 fail ---

def test_hardcoded_windows_path_fails(tmp_path):
    d = make_skill(tmp_path, "---\nname: s\ndescription: d\n---\nbody\n")
    (d / "run.py").write_text('PYTHON = r"C:' + chr(92) + 'Users' + chr(92) + 'j00889192' + chr(92) + 'proj' + chr(92) + '.venv' + chr(92) + 'python.exe"' + chr(10), encoding="utf-8")
    out = run_check(d)
    assert len(out["hardcoded"]) == 1
    assert out["passed"] is False  # 硬编码闸门命中 = error = fail


def test_hardcoded_posix_home_fails(tmp_path):
    d = make_skill(tmp_path, "---\nname: s\ndescription: d\n---\nbody\n")
    (d / "setup.sh").write_text("cd /home/alice/tools && ./install.sh\n", encoding="utf-8")
    out = run_check(d)
    assert len(out["hardcoded"]) == 1


def test_hardcoded_other_agent_ecosystem_fails(tmp_path):
    d = make_skill(tmp_path, "---\nname: s\ndescription: d\n---\nbody\n")
    (d / "SKILL.md").write_text(
        "---\nname: s\ndescription: d\n---\n复制到 ~/.claude/skills/ 使用\n", encoding="utf-8")
    out = run_check(d)
    assert any("claude" in h["pattern"] for h in out["hardcoded"])
    assert out["passed"] is False


def test_relative_and_env_paths_pass(tmp_path):
    d = make_skill(tmp_path, "---\nname: s\ndescription: d\n---\nbody\n")
    (d / "run.py").write_text('DIR = os.environ["SKILL_EVAL_HOME"]\nREL = ".venv/bin/python"\n', encoding="utf-8")
    (d / "x.sh").write_text('cd "$SCRIPT_DIR/../lib" && ./run.sh\n', encoding="utf-8")
    out = run_check(d)
    assert out["hardcoded"] == []
    assert out["passed"] is True
