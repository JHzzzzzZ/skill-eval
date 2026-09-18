"""LLM 评审输出校验（rubric JSON 契约）。Seam: python judge_runner.py --validate <file> -> stdout JSON

契约（judges/*.md 的输出节）：{"score": 0|1|2, "evidence": [str...], "reason": str}
- score < 2 时 evidence 必须非空且全为字符串（必须引用原文）
- score == 2 允许 evidence 为空（"基本无冗余"类结论）
"""
import json
import sys
from pathlib import Path


def validate(payload) -> list:
    errors = []
    if not isinstance(payload, dict):
        return ["顶层必须是 JSON 对象"]
    score = payload.get("score")
    if not isinstance(score, int) or isinstance(score, bool) or score not in (0, 1, 2):
        errors.append("score 必须是 0/1/2 整数")
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not all(isinstance(e, str) for e in evidence):
        errors.append("evidence 必须是字符串数组")
    elif score in (0, 1) and not evidence:
        errors.append("score<2 时 evidence 不能为空（必须引用原文）")
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
