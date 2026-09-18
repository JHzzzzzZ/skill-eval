"""幂等性判定（#15）。Seam: python idem.py <trace1.json> <trace2.json> -> stdout JSON

第二次运行重复第一次已完成步骤的比例高 = 不幂等（如重复下载已存在的文件）。
"""
import json
import sys
from pathlib import Path

IDEMPOTENT_MAX_RATIO = 0.5  # 可配参数（reference.md § 可配参数）


def load_trace(path: str) -> list:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        raise ValueError(f"{path}: 缺少 steps 数组，不是合法 trace")
    for s in data["steps"]:
        if not isinstance(s, dict) or "tool" not in s or "args_hash" not in s:
            raise ValueError(f"{path}: step 缺少 tool/args_hash 字段")
    return data["steps"]


def main():
    if len(sys.argv) != 3:
        print("usage: idem.py <trace1.json> <trace2.json>", file=sys.stderr)
        sys.exit(2)
    steps1 = load_trace(sys.argv[1])
    steps2 = load_trace(sys.argv[2])
    seen1 = {(s["tool"], s["args_hash"]) for s in steps1}
    repeated = sum(1 for s in steps2 if (s["tool"], s["args_hash"]) in seen1)
    total = len(steps2)
    ratio = repeated / total if total else 0.0
    print(json.dumps({
        "total_steps": total,
        "repeated_steps": repeated,
        "ratio": ratio,
        "idempotent": ratio <= IDEMPOTENT_MAX_RATIO,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
