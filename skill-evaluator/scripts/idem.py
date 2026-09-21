"""幂等性判定（#15）。Seam: python idem.py <trace1.json> <trace2.json> [--out <file>] -> stdout JSON

--out：脚本自写 UTF-8 文件（stdout 同步回显），见 static_check.py 说明。
第二次运行重复第一次已完成步骤的比例高 = 不幂等（如重复下载已存在的文件）。
"""
import _console

_console.fix()

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
    args = sys.argv[1:]
    out_path = None
    rest = []
    i = 0
    while i < len(args):
        if args[i] == "--out":
            out_path = Path(args[i + 1]); i += 2
        else:
            rest.append(args[i]); i += 1
    if len(rest) != 2:
        print("usage: idem.py <trace1.json> <trace2.json> [--out <file>]", file=sys.stderr)
        sys.exit(2)
    steps1 = load_trace(rest[0])
    steps2 = load_trace(rest[1])
    seen1 = {(s["tool"], s["args_hash"]) for s in steps1}
    repeated = sum(1 for s in steps2 if (s["tool"], s["args_hash"]) in seen1)
    total = len(steps2)
    ratio = repeated / total if total else 0.0
    out = {
        "total_steps": total,
        "repeated_steps": repeated,
        "ratio": ratio,
        "idempotent": ratio <= IDEMPOTENT_MAX_RATIO,
    }
    text = json.dumps(out, ensure_ascii=False)
    if out_path:
        out_path.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
