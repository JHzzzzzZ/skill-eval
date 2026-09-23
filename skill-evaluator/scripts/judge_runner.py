"""LLM 评审输出校验（rubric JSON 契约）。Seam: python judge_runner.py --validate <file> -> stdout JSON

另提供 --merge：多次单发评审逐项多数票（JUDGE_SAMPLES=3 的落地，ADR-0028）。

契约（judges/*.md 的输出节，#5 逐项计分）：
{"items": [{"name": str, "pass": bool, "quote": str}], "score": 0~1, "evidence": [str...], "reason": str}
- items 必须非空，每项带检查点名、布尔结果、原文引用
- score 由 items 推导（可复现）：pass 项数 / 总项数（0~1，两位小数）；不一致即拒绝
- score < 1 时 evidence 必须非空且全为字符串（必须引用原文）；score == 1 允许 evidence 为空
"""
import _console

_console.fix()

import json
import sys
from pathlib import Path


def score_of_items(items: list) -> float:
    """#5 归一化：pass 项数 / 总项数（0~1，两位小数）。唯一权威口径，rubric 与校验器共用。"""
    passed = sum(1 for it in items if it.get("pass") is True)
    return round(passed / len(items), 2)


def validate(payload) -> list:
    errors = []
    if not isinstance(payload, dict):
        return ["顶层必须是 JSON 对象"]
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        errors.append("items 必须是非空数组（#5 逐项计分）")
        items = None
    else:
        for i, it in enumerate(items):
            if not isinstance(it, dict) or not isinstance(it.get("name"), str) or not it["name"].strip() \
                    or not isinstance(it.get("pass"), bool) \
                    or not isinstance(it.get("quote"), str) or not it["quote"].strip():
                errors.append(f"items[{i}] 必须含非空 name/quote 字符串与布尔 pass")
                break
    score = payload.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
        errors.append("score 必须是 0~1 的数（pass 项数/总项数）")
    elif items:
        expected = score_of_items(items)
        if abs(score - expected) > 1e-9:
            errors.append(f"score={score} 与 items 推导值 {expected} 不一致（#5：pass 项数/总项数）")
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not all(isinstance(e, str) for e in evidence):
        errors.append("evidence 必须是字符串数组")
    elif isinstance(score, (int, float)) and score < 1 and not evidence:
        errors.append("score<1 时 evidence 不能为空（必须引用原文）")
    if not isinstance(payload.get("reason"), str) or not payload["reason"].strip():
        errors.append("reason 必须是非空字符串")
    return errors


def merge(payloads: list) -> dict:
    """多次单发评审逐项多数票（ADR-0028）。

    为什么是多数票而不是“取中位数”：item 的 pass 是布尔，三次投票的中位数就是多数票；
    score 仍由合并后的 items 推导（#5 口径不变）。票型、分歧项与各次得分一并留在输出里，不静默丢。
    缺席项按“未投票”处理（只统计实际出现过的票），全缺席即拒绝。
    """
    if not payloads:
        raise ValueError("merge 至少需要一份评审输出")
    order, votes = [], {}
    for p in payloads:
        for it in p.get("items", []):
            name = it["name"]
            if name not in votes:
                order.append(name)
                votes[name] = {"pass": [], "quotes": []}
            votes[name]["pass"].append(bool(it["pass"]))
            votes[name]["quotes"].append(it.get("quote", ""))
    items, evidence, disagreements = [], [], []
    for name in order:
        v = votes[name]["pass"]
        passed = sum(v) * 2 > len(v)  # 多数票；平票（2 票 1:1）算不通过（保守）
        items.append({"name": name, "pass": passed,
                      "quote": next((q for q, p in zip(votes[name]["quotes"], v) if p),
                                    votes[name]["quotes"][0])})
        if 0 < sum(v) < len(v):
            disagreements.append({"name": name, "pass": sum(v), "votes": len(v)})
        if not passed:
            for q, p in zip(votes[name]["quotes"], v):
                if not p and q and q not in evidence:
                    evidence.append(f"[{name}] {q}")
    score = score_of_items(items)
    reason = (f"{len(payloads)} 次单发评审逐项多数票：{sum(1 for i in items if i['pass'])}/{len(items)} 项通过"
              + (f"；分歧项 {len(disagreements)}" if disagreements else ""))
    return {"items": items, "score": score, "evidence": evidence, "reason": reason,
            "samples": len(payloads),
            "per_sample_scores": [p.get("score") for p in payloads],
            "disagreements": disagreements}


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--merge":
        args = sys.argv[2:]
        out_path = None
        if "--out" in args:
            k = args.index("--out")
            out_path, args = Path(args[k + 1]), args[:k] + args[k + 2:]
        payloads, errors = [], []
        for f in args:
            try:
                payload = json.loads(Path(f).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                errors.append(f"{f}: {e}")
                continue
            errs = validate(payload)
            if errs:
                errors.append(f"{f}: {'; '.join(errs)}")
            else:
                payloads.append(payload)
        if errors or not payloads:
            print(json.dumps({"valid": False, "errors": errors or ["无可合并的有效评审输出"]},
                             ensure_ascii=False))
            return
        merged = merge(payloads)
        errs = validate(merged)
        if errs:
            print(json.dumps({"valid": False, "errors": errs}, ensure_ascii=False))
            return
        if out_path:
            _console.write_text(out_path, json.dumps(merged, ensure_ascii=False, indent=2))
        print(json.dumps({"valid": True, "merged": merged}, ensure_ascii=False))
        return
    if len(sys.argv) != 3 or sys.argv[1] != "--validate":
        print("usage: judge_runner.py --validate <judge输出.json> | --merge <a.json> <b.json> [...] [--out f]",
              file=sys.stderr)
        sys.exit(2)
    try:
        payload = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        errors = validate(payload)
    except json.JSONDecodeError as e:
        errors = [f"非法 JSON: {e}"]
    except OSError as e:
        errors = [f"文件不可读: {e}"]
    print(json.dumps({"valid": not errors, "errors": errors}, ensure_ascii=False))


if __name__ == "__main__":
    main()
