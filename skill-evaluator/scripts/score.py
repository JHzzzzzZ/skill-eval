"""评分计算（#1 P/R/F1，#4 均值方差，#5 必要性 A/B）。
Seam: python score.py <results目录> -> stdout JSON

输入（落盘在同一 results 目录，契约见 reference.md § 中间产物）:
- triggers.json: {"should": [bool...], "should_not": [...], "confusable": [...]}（可选）
- runs.json: [{"tool_calls": int, "tokens": int, "seconds": float}, ...]（可选）
- baseline.json: 同 runs.json 格式，基线 A/B 的"无 skill"运行（可选）

任一输入缺失/非法 → 对应块输出 "skipped"，不崩溃、不打 0 分。
"""
import json
import sys
from pathlib import Path


def load_json(path: Path):
    """读 JSON；文件缺失或非法返回 None（由调用方转 skipped）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def prf1(triggers: dict):
    if not isinstance(triggers, dict) or not isinstance(triggers.get("should"), list)             or not all(isinstance(v, bool) for v in triggers["should"]):
        return "skipped"
    tp = sum(1 for v in triggers["should"] if v)
    fn = sum(1 for v in triggers.get("should", []) if not v)
    fp = sum(1 for k in ("should_not", "confusable") for v in triggers.get(k, []) if v)
    total_positive = tp + fn
    if total_positive == 0:
        # 无"应触发"用例时 recall 无定义，任何数字都会被误读为表现极差
        return "skipped"
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / total_positive
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def stats(runs: list, key: str):
    if not isinstance(runs, list) or not all(isinstance(r, dict) for r in runs):
        return "skipped"
    vals = [r[key] for r in runs if isinstance(r.get(key), (int, float)) and not isinstance(r.get(key), bool)]
    if not vals:
        return "skipped"
    n = len(vals)
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n
    return {"mean": mean, "var": var, "n": n}


def necessity(runs: list, baseline: list):
    # #5: 有 skill (golden) vs 无 skill (baseline) 的 token 均值对比
    if not baseline:
        return "skipped"
    g = stats(runs, "tokens")
    b = stats(baseline, "tokens")
    if g == "skipped" or b == "skipped" or b["mean"] == 0:
        return "skipped"
    return {
        "golden_tokens_mean": g["mean"],
        "baseline_tokens_mean": b["mean"],
        "improvement": (b["mean"] - g["mean"]) / b["mean"],
    }


def main():
    if len(sys.argv) != 2:
        print("usage: score.py <results目录>", file=sys.stderr)
        sys.exit(2)
    d = Path(sys.argv[1])
    triggers = load_json(d / "triggers.json")
    runs = load_json(d / "runs.json") or []
    baseline = load_json(d / "baseline.json") or []
    trigger = prf1(triggers) if triggers is not None else "skipped"
    cost = {k: stats(runs, k) for k in ("tool_calls", "tokens", "seconds")}
    necessity_out = necessity(runs, baseline)
    # passed = 本轮至少有一项真实测量（全部 skipped 则本轮评估无效）
    measured = [trigger != "skipped"] + [v != "skipped" for v in cost.values()]
    out = {"trigger": trigger, "cost": cost, "necessity": necessity_out,
           "passed": any(measured)}
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
