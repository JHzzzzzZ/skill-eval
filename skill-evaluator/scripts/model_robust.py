"""跨模型鲁棒性：同一 skill 换模型跑同一批 case，结果是否一致。Seam: python model_robust.py <results目录> [--out f]

输入约定：<results>/models/<模型名>/trace-<序号>.json —— 序号相同即同一 case（各模型目录的顺序
必须与评测集一致，由 trace_run.py --model 分别跑完后落盘）。默认不跑（成本 ×模型数，ADR-0011），
按需启用。

判定只用确定性信号（不调 LLM）：
- triggered 一致性：各模型是否都触发/都不触发
- 回答相似度：任意两模型回答的字符 3-gram Jaccard ≥ ANSWER_SIM_MIN（全空回答 = 无信息，只看触发面；
  部分空回答 = 不一致，除非空的那个是 early-exit 截断）
- 步数差（tool_calls 极差）只作展示，不计入一致判定（模型步数本就允许不同）

stdout 契约：
{
  "models": [str...], "cases": int, "comparable_cases": int, "incomplete_cases": [int...],
  "case_detail": [{"case", "triggered_agree", "answer_similar", "agree", "tool_calls": {...}}],
  "agreement_ratio": float|null, "robust": bool|null, "skipped": bool, "cost": {...}, "note": str
}

单模型 / 无 models/ 目录 → skipped=true（跨模型对比至少需要 2 个模型），不崩、不打 0 分。
"""
import _console

_console.fix()

import json
import re
import sys
from pathlib import Path

ANSWER_SIM_MIN = 0.5  # 两条回答的 3-gram Jaccard 下限（可配）
AGREEMENT_MIN = 0.8   # 一致 case 占比下限（可配）
NGRAM = 3

TRACE_RE = re.compile(r"^trace-(\d+)\.json$")


def normalize(text: str) -> str:
    return re.sub(r"[^\w]+", "", str(text).lower())


def grams(text: str) -> set:
    n = len(text)
    if n < NGRAM:
        return set()
    return {text[i:i + NGRAM] for i in range(n - NGRAM + 1)}


def jaccard(a: str, b: str) -> float:
    ga, gb = grams(normalize(a)), grams(normalize(b))
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def load_model_dir(d: Path):
    """读一个模型目录下的 trace-<序号>.json，返回 {序号: trace}；非法文件跳过并记 error。"""
    traces, errors = {}, []
    for f in sorted(d.glob("trace-*.json")):
        m = TRACE_RE.match(f.name)
        if not m:
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            errors.append(f"{f.name}: 无法解析（{e}）")
            continue
        if not isinstance(data, dict):
            errors.append(f"{f.name}: 不是 JSON 对象")
            continue
        traces[int(m.group(1))] = data
    return traces, errors


def mean(vals):
    return round(sum(vals) / len(vals), 2) if vals else None


def analyze(results_dir: Path) -> dict:
    models_dir = results_dir / "models"
    errors = []
    if not models_dir.is_dir():
        return {"models": [], "cases": 0, "comparable_cases": 0, "incomplete_cases": [],
                "case_detail": [], "agreement_ratio": None, "robust": None, "skipped": True,
                "cost": {}, "errors": [], "note": "无 models/ 目录，未做跨模型对比"}
    per_model, cost = {}, {}
    for d in sorted(p for p in models_dir.iterdir() if p.is_dir()):
        traces, errs = load_model_dir(d)
        errors += [f"{d.name}/{e}" for e in errs]
        if not traces:
            errors.append(f"{d.name}: 无可用 trace-<序号>.json")
            continue
        per_model[d.name] = traces
        cost[d.name] = {
            "cases": len(traces),
            "tool_calls_mean": mean([t.get("tool_calls", 0) for t in traces.values()]),
            "tokens_mean": mean([t.get("tokens", 0) for t in traces.values()]),
        }
    names = sorted(per_model)
    if len(names) < 2:
        return {"models": names, "cases": sum(len(v) for v in per_model.values()),
                "comparable_cases": 0, "incomplete_cases": [], "case_detail": [],
                "agreement_ratio": None, "robust": None, "skipped": True,
                "cost": cost, "errors": errors,
                "note": f"只有 {len(names)} 个模型的 trace，跨模型对比至少需要 2 个"}

    common = set.intersection(*[set(t) for t in per_model.values()])
    all_idx = set.union(*[set(t) for t in per_model.values()])
    incomplete = sorted(all_idx - common)
    detail, agree_n = [], 0
    for idx in sorted(common):
        traces = [per_model[n][idx] for n in names]
        triggered = [bool(t.get("triggered")) for t in traces]
        triggered_agree = len(set(triggered)) == 1
        answers = [str(t.get("answer") or "") for t in traces]
        empty = [not normalize(a) for a in answers]
        early = [bool(t.get("early_exit")) for t in traces]
        if all(empty):
            # 全空（如触发评测 early-exit 截断）→ 回答面无信息，只按触发面判
            answer_similar = None
        elif any(empty):
            # 部分空：early-exit 截断算"无信息"，其余空回答 = 行为不一致
            answer_similar = None if all(e or not f for f, e in zip(empty, early)) else False
        else:
            sims = [jaccard(answers[i], answers[j])
                    for i in range(len(answers)) for j in range(i + 1, len(answers))]
            answer_similar = min(sims) >= ANSWER_SIM_MIN
        agree = triggered_agree and answer_similar is not False
        agree_n += 1 if agree else 0
        tcs = [t.get("tool_calls", 0) for t in traces]
        detail.append({"case": idx, "triggered_agree": triggered_agree,
                       "answer_similar": answer_similar, "agree": agree,
                       "tool_calls": {n: t.get("tool_calls", 0) for n, t in zip(names, traces)},
                       "tool_calls_spread": (max(tcs) - min(tcs)) if tcs else 0})

    ratio = agree_n / len(common) if common else None
    robust = None if ratio is None else ratio >= AGREEMENT_MIN
    if ratio is None:
        note = "各模型无共同 case 序号，无法对比"
    else:
        note = (f"{len(names)} 个模型 × {len(common)} 个 case：一致 {agree_n}/{len(common)}"
                f"（{ratio:.2f}，阈值 {AGREEMENT_MIN}）→ "
                + ("鲁棒" if robust else "不鲁棒，同一 skill 换模型行为漂移"))
    if incomplete:
        note += f"；{len(incomplete)} 个 case 序号不是所有模型都有，未计入"
    tok = {n: cost[n]["tokens_mean"] for n in names}
    note += f"；token 均值 {tok}"
    return {"models": names, "cases": sum(len(v) for v in per_model.values()),
            "comparable_cases": len(common), "incomplete_cases": incomplete,
            "case_detail": detail, "agreement_ratio": ratio, "robust": robust, "skipped": False,
            "cost": cost, "errors": errors, "note": note}


def main():
    args = sys.argv[1:]
    out_path = None
    rest, i = [], 0
    while i < len(args):
        if args[i] == "--out" and i + 1 < len(args):
            out_path = Path(args[i + 1]); i += 2
        else:
            rest.append(args[i]); i += 1
    if len(rest) != 1:
        print("usage: model_robust.py <results目录> [--out <file>]", file=sys.stderr)
        sys.exit(2)
    d = Path(rest[0])
    if not d.is_dir():
        print(f"results 目录不存在: {d}", file=sys.stderr)
        sys.exit(2)
    out = analyze(d)
    if out_path:
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
