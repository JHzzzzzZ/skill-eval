"""本次运行的成本预估（纯函数；不启动 run、不写盘、不调模型）。

Seam: `import estimate; estimate(tier, ...) -> dict`（Web `/api/estimate` 与页面共用）。

口径（docs/design/usability-next.md §1）：
- T1 = 7 个 rubric 各 1 次判定调用（与 runner.RUBRICS 一致），无 agent 子进程；
- T2A = 触发 case 数个 agent 子进程；早停命中的 case 不调判定，模型调用 0–case 数；
- T2B = 真实输入数 × 2 臂 agent 子进程；有 expect 的 1 次 compare，否则 2 次 pairwise；
- T3 = case 数 × repeat 个 agent 子进程；只有带 expect 的 case 才调 compare；
- T0 无模型调用、无 agent 子进程。

缓存命中的阶段不计入 calls/agent/tokens/minutes（仍列在 cached 里）。token/秒默认值只来自
config.yaml 的 estimate.*；没有历史校准时按默认值估算，并用 assumptions 说明口径。
"""
from __future__ import annotations

import json
from pathlib import Path

import _console

_console.fix()

import _config

TIERS = ("t0", "t1", "t2a", "t2b", "t3")
STAGE_NAMES = {"t0": "T0", "t1": "T1", "t2a": "T2A", "t2b": "T2B", "t3": "T3"}
TRIGGER_GROUPS = ("should", "should-not", "confusable")
JUDGE_CALLS = 7          # T1 的 rubric 数；tests/test_estimate.py 与 runner.RUBRICS 对账

# config.yaml 缺失该项时的代码默认值（保守：宁可估高，不估低）
DEFAULTS = {
    "agent_seconds": 45.0,           # T2B/T3 一次完整任务运行
    "trigger_agent_seconds": 20.0,   # T2A 一次触发 case（多数早停）
    "judge_seconds": 8.0,            # 一次 LLM 判定/对比调用
    "agent_tokens": 12000,           # 一次完整任务运行
    "trigger_agent_tokens": 6000,    # 一次触发 case
    "judge_tokens": 2500,            # 一次 LLM 判定/对比调用
    "t2a_early_exit_ratio": 0.5,     # 早停命中比例：只有未早停的 case 调判定
    "overhead_seconds": 15.0,        # 沙箱副本 + 报告渲染等固定开销
}


def _seconds(key: str) -> float:
    return float(_config.get_float(f"estimate.{key}", float(DEFAULTS[key])))


def _tokens(key: str) -> int:
    return int(_config.get_int(f"estimate.{key}", int(DEFAULTS[key])))


def _prompt_count(directory: Path) -> int:
    """组目录下能解析且 prompt 非空的 *.json 数；坏文件不计（与 evalset_count 同口径）。"""
    if not directory.is_dir():
        return 0
    count = 0
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("prompt"), str) and data["prompt"].strip():
            count += 1
    return count


def _evalset_counts(evalset) -> tuple[int, int, int]:
    """返回 (触发 case 数, results case 数, 带 expect 的 case 数)。"""
    if evalset is None:
        return 0, 0, 0
    root = Path(evalset)
    if not root.is_dir():
        return 0, 0, 0
    triggers = sum(_prompt_count(root / "triggers" / sub) for sub in TRIGGER_GROUPS)
    cases = expect_cases = 0
    for path in sorted((root / "cases").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not (isinstance(data, dict) and isinstance(data.get("prompt"), str)
                and data["prompt"].strip()):
            continue
        cases += 1
        if str(data.get("expect") or "").strip():
            expect_cases += 1
    return triggers, cases, expect_cases


def _normalized_cached(cached_stages) -> set:
    return {str(name).strip().lower() for name in (cached_stages or [])}


def _stage(name: str, *, triggers: int, cases: int, expect_cases: int,
           real_cases: int, real_expects: int, repeat: int, cached: set) -> dict:
    """单阶段的 calls 区间、agent 数、token/秒数（agent 部分）与相对权重。"""
    if name in cached:
        return {"calls_min": 0, "calls_max": 0, "agent_runs": 0, "tokens": 0,
                "agent_seconds": 0.0, "weight": 1}
    if name == "t0":
        # 静态检查没有模型调用；给 1 个权重单位，避免进度条在 T0 完全不动。
        return {"calls_min": 0, "calls_max": 0, "agent_runs": 0, "tokens": 0,
                "agent_seconds": 0.0, "weight": 1}
    if name == "t1":
        seconds = JUDGE_CALLS * _seconds("judge_seconds")
        return {"calls_min": JUDGE_CALLS, "calls_max": JUDGE_CALLS, "agent_runs": 0,
                "tokens": 0, "agent_seconds": 0.0, "weight": max(1, round(seconds))}
    if name == "t2a":
        ratio = min(1.0, max(0.0, _seconds("t2a_early_exit_ratio")))
        seconds = (triggers * _seconds("trigger_agent_seconds")
                   + triggers * (1.0 - ratio) * _seconds("judge_seconds"))
        return {"calls_min": 0, "calls_max": triggers, "agent_runs": triggers,
                "tokens": triggers * _tokens("trigger_agent_tokens"),
                "agent_seconds": triggers * _seconds("trigger_agent_seconds"),
                "weight": max(1, round(seconds))}
    if name == "t2b":
        agent_runs = real_cases * 2
        calls = real_expects + (real_cases - real_expects) * 2
        seconds = agent_runs * _seconds("agent_seconds") + calls * _seconds("judge_seconds")
        return {"calls_min": calls, "calls_max": calls, "agent_runs": agent_runs,
                "tokens": agent_runs * _tokens("agent_tokens"),
                "agent_seconds": agent_runs * _seconds("agent_seconds"),
                "weight": max(1, round(seconds))}
    # t3
    agent_runs = cases * repeat
    seconds = (agent_runs * _seconds("agent_seconds")
               + expect_cases * _seconds("judge_seconds"))
    return {"calls_min": expect_cases, "calls_max": expect_cases, "agent_runs": agent_runs,
            "tokens": agent_runs * _tokens("agent_tokens"),
            "agent_seconds": agent_runs * _seconds("agent_seconds"),
            "weight": max(1, round(seconds))}


def _assumptions(*, tier: str, triggers: int, cases: int, expect_cases: int,
                 real_cases: int, real_expects: int, repeat: int, cached: list,
                 evalset) -> list:
    notes = []
    if cached:
        notes.append("已缓存：" + "、".join(STAGE_NAMES[name] for name in cached)
                     + "（不计入本次调用与耗时）")
    if tier in ("t2a", "t2b", "t3"):        # T2A 在所有 T2 档都会跑
        if evalset is None or triggers == 0:
            notes.append("未提供可用评测集：触发评测按 0 条计")
        else:
            ratio = min(1.0, max(0.0, _seconds("t2a_early_exit_ratio")))
            notes.append(f"触发评测按 {triggers} 条 case、早停命中约 {ratio:.0%} 估算；"
                         f"只有未早停的 case 才调判定（0–{triggers} 次）")
    if tier in ("t2b", "t3"):
        if real_cases:
            notes.append(f"真实输入按 {real_cases} 条 × 2 臂（有 skill / 无 skill）估算；"
                         f"其中 {real_expects} 条有验收标准")
        else:
            notes.append("未提供真实输入：真实输入对比按 0 条计")
    if tier == "t3":
        if cases:
            notes.append(f"完整任务按 {cases} 条 case × {repeat} 次重复估算；"
                         f"其中 {expect_cases} 条有验收标准")
        else:
            notes.append("评测集没有 results case：完整任务按 0 条计")
    notes.append("token 与耗时来自 config.yaml 的 estimate.* 默认值，尚未用历史运行校准")
    notes.append("耗时按串行估算；文档评审实际 4 并发，可能更快")
    return notes


def estimate(tier: str, *, evalset=None, real_cases: int = 0, real_expects: int = 0,
             repeat: int = 3, cached_stages=None) -> dict:
    """按档位与输入规模估算成本；纯函数，只读评测集目录与 config.yaml。"""
    tier = str(tier or "t2a").lower()
    if tier not in TIERS:
        raise ValueError(f"tier 只支持 {', '.join(TIERS)}")
    real_cases = max(0, int(real_cases))
    real_expects = min(real_cases, max(0, int(real_expects)))
    repeat = max(1, int(repeat))
    cached = _normalized_cached(cached_stages)
    triggers, cases, expect_cases = _evalset_counts(evalset)

    stages, weights = [], {}
    calls_min = calls_max = agent_runs = tokens_min = tokens_max = 0
    seconds_min = seconds_max = 0.0
    for name in TIERS[:TIERS.index(tier) + 1]:
        spec = _stage(name, triggers=triggers, cases=cases, expect_cases=expect_cases,
                      real_cases=real_cases, real_expects=real_expects, repeat=repeat,
                      cached=cached)
        calls_min += spec["calls_min"]
        calls_max += spec["calls_max"]
        agent_runs += spec["agent_runs"]
        tokens_min += spec["tokens"] + spec["calls_min"] * _tokens("judge_tokens")
        tokens_max += spec["tokens"] + spec["calls_max"] * _tokens("judge_tokens")
        seconds_min += spec["agent_seconds"] + spec["calls_min"] * _seconds("judge_seconds")
        seconds_max += spec["agent_seconds"] + spec["calls_max"] * _seconds("judge_seconds")
        weights[STAGE_NAMES[name]] = spec["weight"]
        stages.append({"name": STAGE_NAMES[name],
                       "calls": {"min": spec["calls_min"], "max": spec["calls_max"]},
                       "agent_runs": spec["agent_runs"], "weight": spec["weight"]})

    overhead = _seconds("overhead_seconds")
    minutes_min = round((seconds_min + overhead) / 60.0, 1)
    minutes_max = round((seconds_max + overhead) / 60.0, 1)
    cached_used = [name for name in TIERS[:TIERS.index(tier) + 1] if name in cached]
    return {
        "calls": {"min": calls_min, "max": calls_max},
        "agent_runs": agent_runs,
        "tokens": {"min": tokens_min, "max": tokens_max},
        "minutes": {"min": minutes_min, "max": minutes_max},
        "stages": stages,
        "weights": weights,
        "cached": [STAGE_NAMES[name] for name in cached_used],
        "assumptions": _assumptions(tier=tier, triggers=triggers, cases=cases,
                                    expect_cases=expect_cases, real_cases=real_cases,
                                    real_expects=real_expects, repeat=repeat,
                                    cached=cached_used, evalset=evalset),
    }
