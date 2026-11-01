"""config.yaml 子集解析与优先级（ADR-0035）。"""
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _config


def teardown_module(module):
    """测试改过全局缓存；模块结束后恢复仓库 config，避免污染同进程的其他测试。"""
    _config.reload(Path(__file__).parent.parent / "config.yaml")


def test_parse_nested_scalars_comments_and_list():
    data = _config.parse(
        """
# 顶层注释
static:
  name_max_chars: 64   # 行内注释
  description_warn_chars: 384
  ratio: 0.5
  enabled: true
  label: "a # b"
harness:
  available: [pi, "code agent"]
        """
    )
    assert data["static"]["name_max_chars"] == 64
    assert data["static"]["description_warn_chars"] == 384
    assert data["static"]["ratio"] == 0.5
    assert data["static"]["enabled"] is True
    assert data["static"]["label"] == "a # b"
    assert data["harness"]["available"] == ["pi", "code agent"]


def test_parse_rejects_tab_and_four_space_indent():
    with pytest.raises(_config.ConfigError) as exc:
        _config.parse("static:\n\tname_max_chars: 1\n")
    assert ":2:" in str(exc.value)
    with pytest.raises(_config.ConfigError) as exc:
        _config.parse("static:\n    name_max_chars: 1\n")
    assert ":2:" in str(exc.value)


def test_parse_rejects_duplicate_keys():
    with pytest.raises(_config.ConfigError) as exc:
        _config.parse("static:\n  a: 1\n  a: 2\n")
    assert "重复键" in str(exc.value)


def test_missing_file_falls_back_to_defaults(tmp_path):
    _config.reload(tmp_path / "nope.yaml")
    assert _config.get("static.description_warn_chars", 384) == 384
    assert _config.get_int("static.description_warn_chars", 384) == 384


def test_repo_config_has_new_thresholds():
    _config.reload(Path(__file__).parent.parent / "config.yaml")
    assert _config.get_int("static.description_warn_chars", 0) == 384
    assert _config.get_int("static.description_fail_chars", 0) == 1024
    assert _config.get_int("static.body_warn_lines", 0) == 150
    assert _config.get_int("static.body_fail_lines", 0) == 500
    assert _config.get_int("run.trigger_timeout_s", 0) == 90
    assert _config.get_int("run.trigger_max_steps", 0) == 30
    assert _config.get_int("run.trigger_max_tokens", 0) == 120000


def test_type_getters_reject_wrong_types(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("static:\n  a: hello\n  b: true\n", encoding="utf-8")
    _config.reload(path)
    with pytest.raises(_config.ConfigError):
        _config.get_int("static.a", 0)
    with pytest.raises(_config.ConfigError):
        _config.get_int("static.b", 0)


def test_pick_precedence_cli_env_config_default(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("static:\n  n: 2\n", encoding="utf-8")
    _config.reload(path)
    monkeypatch.setenv("SKILL_EVAL_TEST_N", "3")
    assert _config.pick("4", "SKILL_EVAL_TEST_N", "static.n", 1) == "4"
    assert _config.pick(None, "SKILL_EVAL_TEST_N", "static.n", 1) == "3"
    monkeypatch.delenv("SKILL_EVAL_TEST_N")
    assert _config.pick(None, "SKILL_EVAL_TEST_N", "static.n", 1) == 2
    assert _config.pick(None, "SKILL_EVAL_TEST_N", "static.missing", 1) == 1
