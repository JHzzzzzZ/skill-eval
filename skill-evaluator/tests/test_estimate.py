"""scripts/estimate.py：成本预估的计数口径（纯函数，不跑 run、不调模型）。

口径：T1=7 个 rubric；T2A=触发 case 数（判定调用 0–case 数）；T2B=真实输入 ×2 臂；
T3=case ×repeat；缓存阶段不计入 calls/agent/tokens/minutes。
"""
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import estimate  # noqa: E402
import runner  # noqa: E402


def make_evalset(root: Path, *, should=12, should_not=10, confusable=8,
                 cases=10, expect=4) -> Path:
    for group, count in (("should", should), ("should-not", should_not),
                         ("confusable", confusable)):
        d = root / "triggers" / group
        d.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            (d / f"t{i}.json").write_text(
                json.dumps({"prompt": f"prompt {group} {i}"}), encoding="utf-8")
    (root / "cases").mkdir(exist_ok=True)
    for i in range(cases):
        (root / "cases" / f"c{i}.json").write_text(
            json.dumps({"prompt": f"case {i}", "expect": "e" if i < expect else ""}),
            encoding="utf-8")
    return root


def test_tier_stages_and_t1_judge_calls(tmp_path):
    evalset = make_evalset(tmp_path / "v1")
    t0 = estimate.estimate("t0", evalset=evalset)
    assert [s["name"] for s in t0["stages"]] == ["T0"]
    assert t0["calls"] == {"min": 0, "max": 0} and t0["agent_runs"] == 0

    t1 = estimate.estimate("t1", evalset=evalset)
    assert [s["name"] for s in t1["stages"]] == ["T0", "T1"]
    assert t1["calls"] == {"min": estimate.JUDGE_CALLS, "max": estimate.JUDGE_CALLS}
    assert t1["agent_runs"] == 0
    # 7 个 rubric 与 runner.RUBRICS 是同一件事，不能各写一份数字
    assert estimate.JUDGE_CALLS == len(runner.RUBRICS)


def test_t2a_counts_trigger_cases_and_judge_range(tmp_path):
    evalset = make_evalset(tmp_path / "v1", should=12, should_not=10, confusable=8)
    data = estimate.estimate("t2a", evalset=evalset)
    # T1 7 次 + T2A 0–30 次兜底判定
    assert data["calls"] == {"min": 7, "max": 37}
    assert data["agent_runs"] == 30
    assert data["weights"]["T2A"] > data["weights"]["T1"]
    assert 0 <= data["minutes"]["min"] < data["minutes"]["max"]


def test_t2b_expect_branch(tmp_path):
    evalset = make_evalset(tmp_path / "v1")
    data = estimate.estimate("t2b", evalset=evalset, real_cases=4, real_expects=1)
    # 1 条有 expect → 1 次 compare；3 条无 expect → 2 次 pairwise
    t1_calls = estimate.JUDGE_CALLS
    t2b_calls = 1 + 3 * 2
    assert data["calls"]["min"] == t1_calls + t2b_calls          # T2A 下限 0
    assert data["calls"]["max"] == t1_calls + 30 + t2b_calls
    assert data["agent_runs"] == 30 + 4 * 2
    assert data["weights"]["T2B"] > 0

    none = estimate.estimate("t2b", evalset=evalset, real_cases=0, real_expects=0)
    assert none["agent_runs"] == 30          # 只有 T2A 的 case
    assert any("未提供真实输入" in note for note in none["assumptions"])


def test_t3_repeat_scales_agent_runs(tmp_path):
    evalset = make_evalset(tmp_path / "v1", cases=10, expect=4)
    once = estimate.estimate("t3", evalset=evalset, repeat=1)
    thrice = estimate.estimate("t3", evalset=evalset, repeat=3)
    # agent = T2A 30 + T3 case×repeat；calls 只算带 expect 的 case
    assert once["agent_runs"] == 30 + 10
    assert thrice["agent_runs"] == 30 + 30
    assert thrice["calls"]["min"] - once["calls"]["min"] == 0   # compare 按 case 计，不随 repeat 增
    assert thrice["weights"]["T3"] > once["weights"]["T3"]


def test_cached_stages_are_trimmed_and_not_counted(tmp_path):
    evalset = make_evalset(tmp_path / "v1")
    data = estimate.estimate("t2a", evalset=evalset,
                             cached_stages=["T0", "t1", "T3"])
    assert data["cached"] == ["T0", "T1"]      # T3 超出 t2a，不列
    assert data["calls"] == {"min": 0, "max": 30}
    assert data["agent_runs"] == 30             # 只有 T2A
    assert data["weights"]["T0"] == 1 and data["weights"]["T1"] == 1
    assert any("已缓存" in note for note in data["assumptions"])

    full = estimate.estimate("t1", evalset=evalset, cached_stages=["T0", "T1"])
    assert full["calls"] == {"min": 0, "max": 0}
    assert full["agent_runs"] == 0
    assert full["tokens"] == {"min": 0, "max": 0}
    assert full["cached"] == ["T0", "T1"]
    assert full["minutes"]["max"] <= 1          # 只剩固定开销


def test_empty_or_missing_evalset_counts_zero(tmp_path):
    for evalset in (None, tmp_path / "missing"):
        data = estimate.estimate("t3", evalset=evalset)
        # T1 仍会跑 7 个 rubric；T2A/T2B/T3 没有输入
        assert data["calls"] == {"min": estimate.JUDGE_CALLS,
                                 "max": estimate.JUDGE_CALLS}
        assert data["agent_runs"] == 0
        assert any("未提供可用评测集" in note for note in data["assumptions"])
        assert any("完整任务按 0 条" in note for note in data["assumptions"])


def test_t2a_weight_follows_early_exit_ratio(tmp_path, monkeypatch):
    evalset = make_evalset(tmp_path / "v1")
    values = dict(estimate.DEFAULTS)
    monkeypatch.setattr(estimate._config, "get_float",
                        lambda key, default: float(values[key.split(".", 1)[1]]))
    monkeypatch.setattr(estimate._config, "get_int",
                        lambda key, default: int(values[key.split(".", 1)[1]]))
    low = estimate.estimate("t2a", evalset=evalset)
    values["t2a_early_exit_ratio"] = 0.0
    high = estimate.estimate("t2a", evalset=evalset)
    assert high["weights"]["T2A"] > low["weights"]["T2A"]
    assert high["calls"] == low["calls"]        # 命中比例只影响耗时/权重，不缩小判定区间


def test_estimate_reads_config_defaults(tmp_path, monkeypatch):
    evalset = make_evalset(tmp_path / "v1", cases=10, expect=4)
    values = {"agent_seconds": 60.0, "trigger_agent_seconds": 30.0, "judge_seconds": 6.0,
              "agent_tokens": 1000, "trigger_agent_tokens": 500, "judge_tokens": 100,
              "t2a_early_exit_ratio": 1.0, "overhead_seconds": 0.0}
    monkeypatch.setattr(estimate._config, "get_float",
                        lambda key, default: float(values[key.split(".", 1)[1]]))
    monkeypatch.setattr(estimate._config, "get_int",
                        lambda key, default: int(values[key.split(".", 1)[1]]))
    data = estimate.estimate("t3", evalset=evalset, real_cases=2, real_expects=1, repeat=2)
    # agent 30(T2A) + 4(T2B) + 20(T3)；token/秒按注入值算，不读仓库 config
    assert data["agent_runs"] == 54
    calls_min = estimate.JUDGE_CALLS + (1 + 1 * 2) + 4     # T1 + T2B + T3
    assert data["tokens"]["min"] == 30 * 500 + 4 * 1000 + 20 * 1000 + calls_min * 100
    assert data["tokens"]["max"] == data["tokens"]["min"] + 30 * 100
    assert data["minutes"]["min"] == round((30 * 30 + 4 * 60 + 20 * 60 + calls_min * 6) / 60, 1)
    assert data["minutes"]["max"] == round(
        (30 * 30 + 4 * 60 + 20 * 60 + (calls_min + 30) * 6) / 60, 1)


def test_unknown_tier_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        estimate.estimate("t4", evalset=make_evalset(tmp_path / "v1"))
