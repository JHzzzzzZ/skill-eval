"""#14 版本演进：扫描 evalsets/<name>/results/*/report.json，输出跨版本指标变化。
Seam: python evolution.py <evalsets/<name>> [--out <file>]

版本排序按语义化数字（v2 < v10），非字典序。
"""
import json
import re
import sys
from pathlib import Path


SEMVER_RE = re.compile(r"^(v\d+(\.\d+)*|\d+\.\d+(\.\d+)*)$")  # v1/1.2 才算语义序；纯数字串是 hash


def version_key(name_and_dir):
    """语义化版本名按数字排；hash 版本按 report.json 的 generated_at（mtime 会被事后写文件污染）。"""
    name, d = name_and_dir
    if SEMVER_RE.match(name):
        # 全部数字段参与排序（v1.2 < v1.10），缺失段按前缀规则短者在前
        return (1, tuple(int(x) for x in re.findall(r"\d+", name)), name)
    ts = ""
    try:
        r = json.loads((d / "report.json").read_text(encoding="utf-8"))
        ts = str(r.get("generated_at") or "")
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return (0, ts, name)  # ISO 字符串比较即时间序；缺失时为空排最前


def collect(base: Path):
    results = base / "results"
    if not results.is_dir():
        return [], {}
    version_dirs = [(d.name, d) for d in results.iterdir()
                    if d.is_dir() and (d / "report.json").is_file()]
    versions = [name for name, _ in sorted(version_dirs, key=version_key)]
    reports = {}
    for v in versions:
        try:
            reports[v] = json.loads((results / v / "report.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            reports[v] = {"metrics": {}}
    return versions, reports


def main():
    args = sys.argv[1:]
    if not args:
        print("usage: evolution.py <evalsets/<name>> [--out <file>]", file=sys.stderr)
        sys.exit(2)
    base, out_path = None, None
    if "--out" in args:
        oi = args.index("--out")
        if oi + 1 >= len(args):
            print("usage: evolution.py <evalsets/<name>> [--out <file>]", file=sys.stderr)
            sys.exit(2)
        out_path = Path(args[oi + 1])
        positional = [a for i, a in enumerate(args)
                      if a != "--out" and args[i - 1] != "--out"]
        if len(positional) != 1:
            print("usage: evolution.py <evalsets/<name>> [--out <file>]", file=sys.stderr)
            sys.exit(2)
        base = Path(positional[0])
    elif len(args) == 1:
        base = Path(args[0])
    else:
        print("usage: evolution.py <evalsets/<name>> [--out <file>]", file=sys.stderr)
        sys.exit(2)
    if not base.is_dir():
        print(f"目录不存在: {base}", file=sys.stderr)
        sys.exit(2)
    versions, reports = collect(base)

    all_keys = set()
    for r in reports.values():
        all_keys |= set(r.get("metrics", {}).keys())
    changes = []
    for k in sorted(all_keys):
        seq = [(r.get("metrics", {}).get(k, {}).get("verdict") or "missing") for r in
               (reports[v] for v in versions)]
        unique = list(dict.fromkeys(seq))
        if len(unique) > 1:
            changes.append({"metric": k, "from": seq[0], "to": seq[-1], "versions": versions})

    out = {"versions": versions, "changes": changes, "latest_conclusion":
           reports[versions[-1]].get("conclusion") if versions else None}
    if out_path:
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
