"""Seam: 执行适配器契约 / 注册表（阶段 A，docs/design/execution-adapter.md）。

adapter 模块无 CLI：直接 import 测注册表、准备动作与可用性探测的“不抛异常”契约。
CLI 面的 --adapter 行为（build-only / events / unknown）在 test_trace_run.py。
"""
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import adapter_registry as registry  # noqa: E402
from adapter_claude import ClaudeCodeAdapter  # noqa: E402
from adapter_contract import RunRequest  # noqa: E402


# --- 注册表 ---

def test_registry_registers_pi_and_claude_code():
    assert registry.names() == ["claude-code", "pi"]
    assert registry.get().name == "pi"            # 默认载体仍是 pi
    assert registry.get("pi").name == "pi"
    assert registry.get("claude-code").name == "claude-code"


def test_unknown_adapter_raises_instead_of_falling_back():
    with pytest.raises(registry.UnknownAdapterError) as exc:
        registry.get("codex")
    assert "codex" in str(exc.value)
    assert registry.is_available("codex") is False


def test_available_reports_missing_binary_without_raising():
    # 只探测二进制：找不到时返回 (False, 原因)，不抛异常，也不要求测试机安装 claude
    class MissingClaude(ClaudeCodeAdapter):
        binary = "skill-eval-missing-binary"

    ok, detail = MissingClaude().available()
    assert ok is False
    assert "不存在" in detail


# --- claude-code：skill 安装位置（只操作 request.cwd） ---

def _skill(tmp_path: Path) -> Path:
    skill = tmp_path / "my-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: my-skill\ndescription: d\n---\n", encoding="utf-8")
    return skill


def test_claude_prepare_installs_skill_under_cwd(tmp_path):
    skill = _skill(tmp_path)
    wt = tmp_path / "wt"
    wt.mkdir()
    ClaudeCodeAdapter().prepare(RunRequest(prompt="hi", cwd=str(wt), skill_dir=str(skill)))
    assert (wt / ".claude" / "skills" / "my-skill" / "SKILL.md").is_file()


def test_claude_prepare_noop_without_skill(tmp_path):
    skill = _skill(tmp_path)
    wt = tmp_path / "wt"
    wt.mkdir()
    ClaudeCodeAdapter().prepare(RunRequest(prompt="hi", cwd=str(wt), skill_dir=str(skill), no_skill=True))
    assert not (wt / ".claude").exists()


def test_claude_prepare_skill_dir_equal_cwd_does_not_recurse(tmp_path):
    # runner 传的 skill_dir 常等于运行 cwd：装在 cwd/.claude/ 下不能把 .claude 再拷进去
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "SKILL.md").write_text("---\nname: wt\ndescription: d\n---\n", encoding="utf-8")
    ClaudeCodeAdapter().prepare(RunRequest(prompt="hi", cwd=str(wt), skill_dir=str(wt)))
    installed = wt / ".claude" / "skills" / "wt"
    assert (installed / "SKILL.md").is_file()
    assert not (installed / ".claude").exists()


# --- 能力声明 ---

def test_claude_capabilities_declare_no_native_skill_flag():
    caps = ClaudeCodeAdapter.capabilities
    assert caps["steps"] is True and caps["tokens"] is True and caps["early_exit"] is True
    assert caps["skill_flag"] is False
