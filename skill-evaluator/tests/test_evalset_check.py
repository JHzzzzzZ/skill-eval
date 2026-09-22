"""Seam: python scripts/evalset_check.py <evalset目录> --skill <skill目录> [--out f]

触发集质量自检（ADR-0002 的执行检查）：should prompt 照抄 description / 组内近似重复 /
同句同时出现在 should 与 should-not → clean=false，#1 数字可能虚高。
stdout: {"checked", "clean", "echoes_description", "echoes_name", "duplicates", "conflicts", "note"}
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "evalset_check.py"

DESC = "评估一个 skill 包的质量，产出 19 条指标的报告。只在用户明确要求时运行。"
CLEAN_PROMPTS = [
    "帮我看看这个技能到底靠不靠谱",
    "这周五前给我一份体检结论，我想知道该先改哪里",
    "我写了个说明书，你帮我挑挑毛病",
]


def make_skill(tmp_path, description=DESC, name="skill-evaluator"):
    d = tmp_path / "skill"
    d.mkdir(exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# S\n\n正文\n", encoding="utf-8")
    return d


def make_evalset(tmp_path, should=(), should_not=(), confusable=()):
    d = tmp_path / "evalsets" / "sk" / "v1"
    for group, prompts in (("should", should), ("should-not", should_not), ("confusable", confusable)):
        g = d / "triggers" / group
        g.mkdir(parents=True, exist_ok=True)
        for i, p in enumerate(prompts):
            (g / f"s{i}.json").write_text(json.dumps({"prompt": p}, ensure_ascii=False), encoding="utf-8")
    return d


def run(evalset, skill):
    r = subprocess.run([sys.executable, str(SCRIPT), str(evalset), "--skill", str(skill)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# --- 干净触发集 ---

def test_clean_evalset(tmp_path):
    ev = make_evalset(tmp_path, should=CLEAN_PROMPTS,
                      should_not=["明天天气怎么样", "帮我订一张去上海的票"])
    out = run(ev, make_skill(tmp_path))
    assert out["clean"] is True
    assert out["checked"] == 5
    assert out["echoes_description"] == [] and out["duplicates"] == [] and out["conflicts"] == []


# --- 照抄 description ---

def test_echo_of_description_flagged(tmp_path):
    ev = make_evalset(tmp_path, should=[DESC])
    out = run(ev, make_skill(tmp_path))
    assert out["clean"] is False
    assert len(out["echoes_description"]) == 1
    assert out["echoes_description"][0]["coverage"] == 1.0
    assert "照抄" in out["note"] or "高度重合" in out["note"]


def test_punctuation_rewrite_still_flagged(tmp_path):
    # 只换标点/空格不算改写：normalize 之后仍是同一串
    ev = make_evalset(tmp_path, should=["评估一个skill包的质量, 产出19条指标的报告! 只在用户明确要求时运行"])
    out = run(ev, make_skill(tmp_path))
    assert out["clean"] is False
    assert out["echoes_description"][0]["coverage"] >= 0.9


def test_name_echo_flagged(tmp_path):
    ev = make_evalset(tmp_path, should=["帮我用 skill-evaluator 这个技能跑一遍"])
    out = run(ev, make_skill(tmp_path))
    assert out["clean"] is False
    assert len(out["echoes_name"]) == 1


# --- 覆盖度虚高：组内重复 / 跨组冲突 ---

def test_duplicate_prompts_flagged(tmp_path):
    ev = make_evalset(tmp_path, should=["帮我看看这个技能到底靠不靠谱",
                                        "帮我看看这个技能到底靠不靠得住"])
    out = run(ev, make_skill(tmp_path))
    assert out["clean"] is False
    assert len(out["duplicates"]) == 1
    assert out["duplicates"][0]["coverage"] >= 0.8


def test_conflict_between_groups_flagged(tmp_path):
    same = "帮我看看这个技能到底靠不靠谱"
    ev = make_evalset(tmp_path, should=[same], should_not=[same])
    out = run(ev, make_skill(tmp_path))
    assert out["clean"] is False
    assert len(out["conflicts"]) == 1


# --- 不做过度判定 ---

def test_no_triggers_dir_is_unjudged(tmp_path):
    d = tmp_path / "evalsets" / "sk" / "v1"
    d.mkdir(parents=True)
    out = run(d, make_skill(tmp_path))
    assert out["checked"] == 0
    assert out["clean"] is None  # 无触发集 → 不判质量，不是"干净"
    assert "#1" in out["note"]


def test_invalid_json_recorded_not_crash(tmp_path):
    ev = make_evalset(tmp_path, should=CLEAN_PROMPTS)
    (ev / "triggers" / "should" / "bad.json").write_text("{不是 json", encoding="utf-8")
    out = run(ev, make_skill(tmp_path))
    assert out["checked"] == 3
    assert len(out["errors"]) == 1
    assert "无法解析" in out["note"]


def test_missing_evalset_dir_exits_2(tmp_path):
    r = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path / "nope"),
                        "--skill", str(make_skill(tmp_path))],
                       capture_output=True, text=True)
    assert r.returncode == 2
    assert "traceback" not in r.stderr.lower()


def test_skill_without_skill_md_exits_2(tmp_path):
    ev = make_evalset(tmp_path, should=CLEAN_PROMPTS)
    empty = tmp_path / "empty_skill"
    empty.mkdir()
    r = subprocess.run([sys.executable, str(SCRIPT), str(ev), "--skill", str(empty)],
                       capture_output=True, text=True)
    assert r.returncode == 2
    assert "traceback" not in r.stderr.lower()


def test_missing_skill_arg_exits_2(tmp_path):
    ev = make_evalset(tmp_path, should=CLEAN_PROMPTS)
    r = subprocess.run([sys.executable, str(SCRIPT), str(ev)], capture_output=True, text=True)
    assert r.returncode == 2
    assert "usage" in r.stderr
