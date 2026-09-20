"""LLM 评审输出校验（rubric JSON 契约）。Seam: python judge_runner.py --validate <file> -> stdout JSON

契约（judges/*.md 的输出节，#5 逐项计分）：
{"items": [{"name": str, "pass": bool, "quote": str}], "score": 0~1, "evidence": [str...], "reason": str}
- items 必须非空，每项带检查点名、布尔结果、原文引用
- score 由 items 推导（可复现）：pass 项数 / 总项数（0~1，两位小数）；不一致即拒绝
- score < 1 时 evidence 必须非空且全为字符串（必须引用原文）；score == 1 允许 evidence 为空
"""
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


def main():
    if len(sys.argv) != 3 or sys.argv[1] != "--validate":
        print("usage: judge_runner.py --validate <judge输出.json>", file=sys.stderr)
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
