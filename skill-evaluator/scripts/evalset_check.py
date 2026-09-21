"""评测集条数闸门（冻结前置）。Seam: python evalset_check.py <evalsets/<name>/vN> [--out <file>]
[--min N] [--min-should N] [--min-not N] [--min-confusable N] -> stdout JSON

触发集三组（triggers/should、triggers/should-not、triggers/confusable）各 ≥ 最小条数，
不达标不得进入人工审核（reference.md § 评测集冻结）。cases 条数不在本闸门范围。

条数 = 组目录下能解析且 prompt 非空的 *.json 数（坏文件不计，但记入 issues 提示）。

最小条数配置优先级：--min-组参 > --min > 环境变量 SKILL_EVAL_TRIGGER_MIN > 默认 10。
输出 {"counts", "minimums", "passed", "issues"}；passed=False 仅体现在 JSON，退出码恒 0（对齐 static_check）。
"""
import _console

_console.fix()

import json
import os

import sys
from pathlib import Path

DEFAULT_MIN = 10  # 三组各自的默认最小条数（可配：环境变量 / CLI，见模块注释）

GROUPS = {"should": "triggers/should", "should_not": "triggers/should-not",
          "confusable": "triggers/confusable"}


def count_group(d: Path):
    """返回 (有效条数, 坏文件名列表)：能解析且 prompt 非空才计入。"""
    ok, bad = 0, []
    if not d.is_dir():
        return 0, ["目录不存在"]
    for f in sorted(d.glob("*.json")):
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            bad.append(f.name)
            continue
        if isinstance(j, dict) and isinstance(j.get("prompt"), str) and j["prompt"].strip():
            ok += 1
        else:
            bad.append(f.name)
    return ok, bad


def emit(out: dict, out_path) -> None:
    text = json.dumps(out, ensure_ascii=False)
    if out_path:
        out_path.write_text(text, encoding="utf-8")
    print(text)


def main():
    argv = sys.argv[1:]
    out_path = None
    mins = {}
    rest = []
    i = 0
    while i < len(argv):
        if argv[i] == "--out":
            out_path = Path(argv[i + 1]); i += 2
        elif argv[i] == "--min":
            v = int(argv[i + 1]); mins = {k: v for k in GROUPS}; i += 2
        elif argv[i] == "--min-should":
            mins["should"] = int(argv[i + 1]); i += 2
        elif argv[i] == "--min-not":
            mins["should_not"] = int(argv[i + 1]); i += 2
        elif argv[i] == "--min-confusable":
            mins["confusable"] = int(argv[i + 1]); i += 2
        else:
            rest.append(argv[i]); i += 1
    if len(rest) != 1 or not Path(rest[0]).is_dir():
        print("usage: evalset_check.py <evalsets/<name>/vN> [--out <file>] [--min N] "
              "[--min-should N] [--min-not N] [--min-confusable N]", file=sys.stderr)
        sys.exit(2)

    env = os.environ.get("SKILL_EVAL_TRIGGER_MIN")
    env_v = int(env) if env and env.strip().isdigit() else None
    minimums = {k: mins.get(k, env_v if env_v is not None else DEFAULT_MIN) for k in GROUPS}

    counts, issues = {}, []
    for key, sub in GROUPS.items():
        n, bad = count_group(Path(rest[0]) / sub)
        counts[key] = n
        for name in bad:
            issues.append(f"{sub}/{name}：不是合法的 {{\"prompt\": ...}} 条目，不计入条数")
        if n < minimums[key]:
            issues.append(f"{sub} 仅 {n} 条，少于最小条数 {minimums[key]}")

    # meta.counts 新鲜度：冻结后扩条未回写 → warnings 提示（不阻断，passed 只由闸门条数决定）
    warnings = []
    meta_path = Path(rest[0]) / "meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            meta = None
        recorded = (meta or {}).get("counts")
        if isinstance(recorded, dict):
            for key in GROUPS:
                if key in recorded and recorded[key] != counts[key]:
                    warnings.append(f"meta.json counts[{key}]={recorded[key]} 与实际 {counts[key]} 不符"
                                    "（冻结后扩条未回写）")

    out = {"counts": counts, "minimums": minimums, "warnings": warnings,
           "passed": not issues, "issues": issues}
    emit(out, out_path)


if __name__ == "__main__":
    main()
