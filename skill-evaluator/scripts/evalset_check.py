"""触发集质量自检（#1 的可信度闸门）。Seam: python evalset_check.py <evalset目录> --skill <skill目录> [--out f]

为什么需要：should 类 prompt 若照抄 description 措辞，触发评测测的是"字面匹配"而非"语义触发"，
precision/recall 会虚高（ADR-0002 早已写下的规则，本脚本是它的执行检查）。用确定性字符 3-gram
覆盖率排查四类问题，不调 LLM。

stdout 契约（字段恒输出）：
{
  "checked": int,                     # 实际检查的 prompt 条数
  "clean": bool|null,                 # null = 无可检查项（无 triggers/），true/false = 结论
  "echoes_description": [{"file", "prompt", "coverage"}],
  "echoes_name":        [{"file", "prompt", "name"}],
  "duplicates":         [{"a", "b", "coverage"}],      # 同一组内近似重复 → 覆盖度虚高
  "conflicts":          [{"should", "should_not", "coverage"}],  # 同句同时出现在两组 → 自相矛盾
  "note": str
}

阈值是启发式（可配参数，reference.md § 可配参数），命中即"疑似"，不阻断评估，由 report.py 把 #1 降级为 warn。
"""
import _console

_console.fix()

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from static_check import parse_frontmatter

ECHO_COVERAGE_MAX = 0.6  # prompt 对 description 的 3-gram 覆盖率上限（可配）
DUP_COVERAGE_MAX = 0.8   # 组内两条 prompt 的互相覆盖率上限（可配）
NGRAM = 3

GROUPS = ("should", "should-not", "confusable")


def normalize(text: str) -> str:
    """小写 + 只留字母数字与汉字（去标点/空白），让"换标点改写"照样被识别为照抄。"""
    return re.sub(r"[^\w]+", "", str(text).lower())


def grams(text: str) -> set:
    n = len(text)
    if n < NGRAM:
        return set()
    return {text[i:i + NGRAM] for i in range(n - NGRAM + 1)}


def coverage(part: str, whole: str) -> float:
    """part 的 3-gram 落在 whole 中的比例（0~1）；任一侧过短则 0。"""
    pg, wg = grams(part), grams(whole)
    if not pg or not wg:
        return 0.0
    return len(pg & wg) / len(pg)


def load_group(triggers_dir: Path, group: str, errors: list):
    """读 triggers/<group>/*.json 的 prompt；非法文件计入 errors，不崩。"""
    d = triggers_dir / group
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            errors.append(f"{group}/{f.name}: 无法解析（{e}）")
            continue
        prompt = data.get("prompt") if isinstance(data, dict) else None
        if not isinstance(prompt, str) or not prompt.strip():
            errors.append(f"{group}/{f.name}: 缺 prompt 字段")
            continue
        out.append({"file": f"{group}/{f.name}", "prompt": prompt, "norm": normalize(prompt)})
    return out


def find_duplicates(items: list, max_cov: float):
    """组内近似重复：覆盖度高的两两组合（取较高方向，避免长句吞短句）。"""
    out = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            cov = max(coverage(a["norm"], b["norm"]), coverage(b["norm"], a["norm"]))
            if cov >= max_cov:
                out.append({"a": a["file"], "b": b["file"], "coverage": round(cov, 2)})
    return out


def find_conflicts(should: list, should_not: list, max_cov: float):
    """同一句（或近同一句）同时出现在 should 与 should-not → 触发集自相矛盾，指标无意义。"""
    out = []
    for a in should:
        for b in should_not:
            cov = max(coverage(a["norm"], b["norm"]), coverage(b["norm"], a["norm"]))
            if cov >= max_cov:
                out.append({"should": a["file"], "should_not": b["file"], "coverage": round(cov, 2)})
    return out


def check(evalset_dir: Path, skill_dir: Path) -> dict:
    errors = []
    triggers = evalset_dir / "triggers"
    groups = {g: load_group(triggers, g, errors) for g in GROUPS}
    checked = sum(len(v) for v in groups.values())

    skill_md = skill_dir / "SKILL.md"
    try:
        meta, _ = parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        meta = {}
    desc_norm = normalize(meta.get("description") or "")
    name_norm = normalize(meta.get("name") or "")

    echoes_desc, echoes_name = [], []
    for item in groups["should"]:  # 只有"应触发"组照抄才影响指标；should-not 照抄反而是正常负例
        if desc_norm:
            cov = coverage(item["norm"], desc_norm)
            if cov >= ECHO_COVERAGE_MAX:
                echoes_desc.append({"file": item["file"], "prompt": item["prompt"],
                                    "coverage": round(cov, 2)})
        if name_norm and name_norm in item["norm"]:
            echoes_name.append({"file": item["file"], "prompt": item["prompt"], "name": meta.get("name")})

    duplicates = []
    for g in GROUPS:
        duplicates += find_duplicates(groups[g], DUP_COVERAGE_MAX)
    conflicts = find_conflicts(groups["should"], groups["should-not"], DUP_COVERAGE_MAX)

    if checked == 0:
        clean, note = None, "无可检查的触发 prompt（triggers/ 缺失或为空），#1 触发集质量未判"
    else:
        problems = []
        if echoes_desc:
            problems.append(f"{len(echoes_desc)} 条 should prompt 与 description 高度重合（≥{ECHO_COVERAGE_MAX}）")
        if echoes_name:
            problems.append(f"{len(echoes_name)} 条 should prompt 直接含 skill name")
        if duplicates:
            problems.append(f"{len(duplicates)} 组组内近似重复 prompt")
        if conflicts:
            problems.append(f"{len(conflicts)} 组同句同时出现在 should 与 should-not")
        clean = not problems
        note = (f"检查 {checked} 条 prompt：无照抄/重复/冲突" if clean
                else f"检查 {checked} 条 prompt：" + "；".join(problems) + "——#1 指标可能虚高，建议改写触发集")
    if errors:
        note += f"（另有 {len(errors)} 条无法解析，见 errors）"

    return {"checked": checked, "clean": clean,
            "echoes_description": echoes_desc, "echoes_name": echoes_name,
            "duplicates": duplicates, "conflicts": conflicts,
            "errors": errors, "note": note}


def main():
    args = sys.argv[1:]
    evalset_dir = skill_dir = out_path = None
    rest, i = [], 0
    while i < len(args):
        if args[i] == "--skill" and i + 1 < len(args):
            skill_dir = Path(args[i + 1]); i += 2
        elif args[i] == "--out" and i + 1 < len(args):
            out_path = Path(args[i + 1]); i += 2
        else:
            rest.append(args[i]); i += 1
    if len(rest) != 1 or skill_dir is None:
        print("usage: evalset_check.py <evalset目录> --skill <skill目录> [--out <file>]",
              file=sys.stderr)
        sys.exit(2)
    evalset_dir = Path(rest[0])
    if not evalset_dir.is_dir():
        print(f"评测集目录不存在: {evalset_dir}", file=sys.stderr)
        sys.exit(2)
    if not (skill_dir / "SKILL.md").is_file():
        print(f"skill 目录无 SKILL.md: {skill_dir}", file=sys.stderr)
        sys.exit(2)
    out = check(evalset_dir, skill_dir)
    if out_path:
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
