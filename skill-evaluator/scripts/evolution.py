"""#14 版本演进：扫描 evalsets/<name>/results/*/report.json，输出跨版本指标变化 + 评估器指纹可比性。
Seam: python evolution.py <evalsets/<name>> [--out <file>]

版本排序按语义化数字（v2 < v10），非字典序。
评估器指纹（ADR-0010）不同的版本之间，指标差异不构成被测 skill 的回归证据 → comparable=false。
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
        nums = re.findall(r"\d+", name)
        return (1, int(nums[0]), name)
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


def evaluator_versions(results: Path, versions, reports) -> dict:
    """每个版本由哪个评估器指纹产生：优先 report.json，回退同目录 meta.json（旧报告两者都可能没有）。"""
    out = {}
    for v in versions:
        ev = reports.get(v, {}).get("evaluator")
        ver = ev.get("version") if isinstance(ev, dict) else None
        if not ver:
            try:
                meta = json.loads((results / v / "meta.json").read_text(encoding="utf-8"))
                ev2 = meta.get("evaluator")
                ver = ev2.get("version") if isinstance(ev2, dict) else None
            except (OSError, json.JSONDecodeError, AttributeError, ValueError):
                ver = None
        out[v] = ver
    return out


def comparability(versions, ev_versions):
    """指纹不一致 → 不可比；部分缺失 → 可比性未知（None）；全一致 → True。"""
    known = {v: k for v, k in ev_versions.items() if k}
    if not versions:
        return None, "无版本可比"
    if len(set(known.values())) > 1:
        detail = "，".join(f"{v}={k}" for v, k in known.items())
        return False, f"评估器指纹不一致（{detail}）：指标差异可能来自评估器变更，不构成被测 skill 的回归证据"
    if len(known) < len(versions):
        return None, f"{len(versions) - len(known)} 个版本未记录评估器指纹（旧报告），可比性未知"
    return True, f"所有版本同一评估器指纹（{next(iter(known.values()))}），指标差异可比"


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
    ev_versions = evaluator_versions(base / "results", versions, reports)
    comparable, ev_note = comparability(versions, ev_versions)

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
           reports[versions[-1]].get("conclusion") if versions else None,
           "evaluator_versions": ev_versions, "comparable": comparable, "evaluator_note": ev_note}
    if out_path:
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
