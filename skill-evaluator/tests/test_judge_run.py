"""judge_run.py 的输入收集与 prompt 构造（不调用真实 LLM）。"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import judge_run


def make_skill(tmp_path: Path) -> Path:
    d = tmp_path / "skill"
    (d / "scripts").mkdir(parents=True)
    (d / "tests").mkdir()
    (d / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n正文\n", encoding="utf-8")
    (d / "reference.md").write_text("细节\n", encoding="utf-8")
    (d / "scripts" / "x.py").write_text("print(1)\n", encoding="utf-8")
    (d / "tests" / "secret.md").write_text("不应进 prompt\n", encoding="utf-8")
    return d


def test_collect_files_excludes_tests_and_puts_skill_first(tmp_path):
    files = judge_run.collect_files(make_skill(tmp_path), max_chars=10000)
    paths = [f["path"] for f in files]
    assert paths[0] == "SKILL.md"
    assert "scripts/x.py" in paths
    assert not any(p.startswith("tests/") for p in paths)


def test_build_prompt_contains_rubric_and_files(tmp_path):
    files = judge_run.collect_files(make_skill(tmp_path), max_chars=10000)
    prompt = judge_run.build_prompt("RUBRIC", files)
    assert "RUBRIC" in prompt and "### SKILL.md" in prompt and "print(1)" in prompt


def test_skill_md_is_always_first_even_with_tiny_budget(tmp_path):
    d = make_skill(tmp_path)
    (d / "big.md").write_text("x" * 5000, encoding="utf-8")
    files = judge_run.collect_files(d, max_chars=120)
    assert files[0]["path"] == "SKILL.md"
    assert any(f["path"] == "SKILL.md" for f in files)


def test_missing_skill_md_is_an_error(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        judge_run.collect_files(tmp_path, max_chars=100)


def test_collect_files_truncates_at_limit(tmp_path):
    d = make_skill(tmp_path)
    (d / "big.md").write_text("x" * 5000, encoding="utf-8")
    files = judge_run.collect_files(d, max_chars=200)
    assert sum(len(f["text"]) for f in files) <= 200 + 40
