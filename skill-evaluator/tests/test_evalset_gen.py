"""evalset_gen.py 的纯函数：prompt 与条目归一化（不调 LLM）。"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import evalset_gen


def test_build_prompt_includes_skill_and_count():
    prompt = evalset_gen.build_prompt("SKILL-BODY", 7)
    assert "SKILL-BODY" in prompt and "7 条" in prompt
    assert "should_not" in prompt and "cases" in prompt


def test_items_normalizes_strings_and_dicts():
    items = evalset_gen._items(["a", {"prompt": " b "}, {"x": 1}, "  "])
    assert items == [{"prompt": "a"}, {"prompt": "b"}]
