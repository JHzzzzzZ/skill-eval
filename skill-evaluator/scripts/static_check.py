"""静态检查（#2/#7/#13）。Seam: python static_check.py <skill目录> [--exclude <文件>] [--out <file>] -> stdout JSON

--out：脚本自写 UTF-8 文件（stdout 同步回显）——Windows 控制台重定向会产 GBK，
中间产物一律用 --out，禁止 shell 重定向（ADR-0009 同源原则）。

stdout 契约（字段恒输出）：
{
  "name": str|null, "description": str|null, "description_tokens": int,
  "invoke": {"allowed": [str...], "resolved": str, "note": str},   # resolved: both|human（映射 disable-model-invocation）
  "dangerous": [{"pattern": str, "line": int, "file": str, "text": str}],
  "hardcoded": [{"pattern": str, "line": int, "file": str, "text": str}],   # 可移植性闸门（ADR-0008）
  "scan_excluded_dirs": [str],   # 闸门豁免目录（ADR-0011：tests/ 不属运行面）
  "stale_refs": [str],           # SKILL.md 声明但包内不存在的路径（依赖新鲜度，warning 级）
  "errors": [str], "warnings": [str], "issues": [str],   # issues = errors + warnings
  "passed": bool          # errors 为空即 True（危险命令/缺 name 都算 error）
}

局限：手写 frontmatter 解析只支持单行 key: value（不支持多行/嵌套列表），不要扩展。
"""
import _console

_console.fix()

import json
import re
import sys
from pathlib import Path

# 可配置常量（reference.md § 可配参数）
DESCRIPTION_TOKEN_LIMIT = 100
NAME_MAX_CHARS = 64

INVOKE_VALUES = {"human", "both"}  # pi 真实三态归约：缺省/false=both（模型+用户）；disable-model-invocation:true=human

# 闸门豁免目录（ADR-0011）：仅扫描运行面。tests/ 是测试夹具；evalsets/uploads/.sandbox
# 是评估产物/存档（trace、报告、只读副本，含评估时机器路径），都不随 skill 运行
SCAN_EXEMPT_DIRS = frozenset({"tests", "evalsets", "uploads", ".sandbox"})

DANGEROUS_PATTERNS = [
    r"rm\s+-rf?\b",
    r"\bdel\s+/[qs]",
    r"remove-item\s+.*-recurse",   # PowerShell
    r"git\s+push\s+(-f|--force)",
    r"git\s+reset\s+--hard",
    r"git\s+clean\s+-[a-z]*f",
    r"\b" + "su" + "do\b",   # 拼接避免模式表自匹配（ADR-0011：表是数据不是行为）
    r"curl\s+[^|]*\|\s*(\b" + "su" + "do\b\s+)?(ba)?sh",
    r"chmod\s+777",
    r"\bformat\b\s+[a-zA-Z]:",
    r"drop\s+table\b",
]

# 可移植性闸门（ADR-0008）：宿主环境硬编码 → 整体 fail。
# 只查硬编码，不查"声明给某 agent 用"（#7 调用方式与通用性无关）。
# 排除：相对路径与环境变量引用本就不匹配这些模式。具体路径示例不写入本文件注释，
# 免得模式表自匹配（ADR-0011：表是数据不是行为）。
# 作用域（ADR-0011）：闸门只扫运行面——tests/ 内的夹具（tmp_path 运行时路径、
# 危险命令用例）是测试产物，不随 skill 运行，默认豁免。
HOST_HARDCODE_PATTERNS = [
    r"[A-Za-z]:[\\/](?:Users|home)[\\/]",   # Windows 盘符开头的绝对用户路径
    r"(?<![\w.])/(?:Users|home)/[\w.-]+",   # POSIX 绝对用户目录
    r"(?<![\w.])\.(?:claude|cursor|codex)\b",  # 他方 agent 生态目录（点号开头）
    r"(?<![\w.])~[\\/]\.(?:claude|cursor|codex)\b",  # 波号下的他方生态目录
]


def parse_frontmatter(text: str):
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip('"')
    return meta, text[m.end():]


def token_estimate(text: str) -> int:
    # 粗估：中文按 1 字 ~1 token，英文按 4 字符 ~1 token
    zh = len(re.findall(r"[\u4e00-\u9fff]", text))
    rest = len(text) - zh
    return zh + max(0, rest) // 4


# 依赖新鲜度（ADR-0009 延伸）：SKILL.md 声明的路径引用应能定位到包内文件。
# 只查“声明了但不存在”（文档漂移），不查语义对错——那是 #8 语义面（judges/deps.md）的事。
REF_EXTS = ".py .mjs .js .ts .md .json .sh .ps1 .cmd .bat .toml .yaml .yml".split()


def find_stale_refs(skill_dir: Path, body: str):
    refs, seen = [], set()

    def check(ref, require_slash):
        ref = ref.strip().strip(".,;:)）")
        if (not ref or ref in seen or "*" in ref or "<" in ref or "://" in ref
                or ref.startswith(("/", "~", "http")) or not re.search(r"\.\w+$", ref)):
            return
        if require_slash and "/" not in ref:
            return  # 裸文件名多为运行产物（如 process.json），不查
        ext = ref.rsplit(".", 1)[-1].lower()
        if f".{ext}" not in REF_EXTS:
            return
        seen.add(ref)
        if not (skill_dir / ref).exists():
            refs.append(ref)

    for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", body):   # markdown 链接：意图明确，逐个查
        check(target.split("#", 1)[0], require_slash=False)
    for code in re.findall(r"`([^`]+)`", body):                  # 行内代码：可能含命令，拆词后只查带斜杠的路径
        for tok in code.split():
            check(tok, require_slash=True)
    return sorted(set(refs))


def scan_patterns(skill_dir: Path, patterns, exclude_paths=None):
    hits = []
    for f in sorted(skill_dir.rglob("*")):
        if not f.is_file() or f.suffix not in (".py", ".sh", ".ps1", ".cmd", ".bat", ".js", ".md", ".json", ".toml", ".yaml", ".yml"):
            continue
        # ADR-0011：tests/ 是测试夹具面，不属于运行面，闸门豁免
        rel = f.relative_to(skill_dir)
        if rel.parts[0] in SCAN_EXEMPT_DIRS:
            continue
        if exclude_paths and any(f.resolve().is_relative_to(e) for e in exclude_paths):
            continue
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            for p in patterns:
                if re.search(p, line, re.IGNORECASE):
                    hits.append({"pattern": p, "line": i, "file": f.name, "text": line.strip()})
    return hits


def emit(out: dict, out_path) -> None:
    """stdout 同步回显；--out 存在时另写 UTF-8 文件（禁止 shell 重定向的 GBK 风险）。"""
    text = json.dumps(out, ensure_ascii=False)
    if out_path:
        out_path.write_text(text, encoding="utf-8")
    print(text)


def main():
    argv = sys.argv[1:]
    excludes = []
    out_path = None
    i = 0
    rest = []
    while i < len(argv):
        if argv[i] == "--exclude":
            excludes.append(argv[i + 1]); i += 2
        elif argv[i] == "--out":
            out_path = Path(argv[i + 1]); i += 2
        else:
            rest.append(argv[i]); i += 1
    if len(rest) != 1:
        print("usage: static_check.py <skill目录> [--exclude <文件>]... [--out <file>]", file=sys.stderr)
        sys.exit(2)
    skill_dir = Path(rest[0])
    exclude_paths = {Path(e).resolve() for e in excludes}
    out = {"name": None, "description": None, "description_tokens": 0,
           "invoke": {"allowed": sorted(INVOKE_VALUES), "resolved": "both", "note": ""},
           "dangerous": [], "hardcoded": [], "errors": [], "warnings": [], "issues": [], "passed": True}
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        out["errors"].append("SKILL.md 不存在：不是合法 skill 包")
        out["passed"] = False
        out["issues"] = list(out["errors"])
        emit(out, out_path)
        return

    try:
        text = skill_md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        text = ""
    meta, body = parse_frontmatter(text)
    out["name"] = meta.get("name")
    out["description"] = meta.get("description")

    if not out["name"]:
        out["errors"].append("frontmatter 缺 name")
    elif len(out["name"]) > NAME_MAX_CHARS:
        out["warnings"].append(f"name {len(out['name'])} 字符，超过 {NAME_MAX_CHARS}（#2）")
    if not out["description"]:
        out["errors"].append("frontmatter 缺 description")
        out["passed"] = False
    else:
        out["description_tokens"] = token_estimate(out["description"])
        if out["description_tokens"] > DESCRIPTION_TOKEN_LIMIT:
            out["warnings"].append(
                f"description 约 {out['description_tokens']} token，超过阈值 {DESCRIPTION_TOKEN_LIMIT}（#2）")

    # #7 调用方式：映射 pi 真实 frontmatter 字段 disable-model-invocation（ADR-0008）。
    # 不再检查虚构的 invoke 字段：pi/Agent Skills 规范无此字段，检查恒空转。
    raw = (meta.get("disable-model-invocation") or "").strip().strip('"').lower()
    if raw in ("true", "yes", "1"):
        out["invoke"]["resolved"] = "human"
        out["invoke"]["note"] = "disable-model-invocation=true：对模型隐藏，仅 /skill:name 手动调用"
    elif raw in ("", "false", "0", "no"):
        out["invoke"]["note"] = "缺省/false：模型按 description 触发，用户也可手动调用"
    else:
        out["warnings"].append(
            f"disable-model-invocation 取值 '{raw}' 不是布尔（pi 将按未知字段忽略）（#7）")

    out["dangerous"] = scan_patterns(skill_dir, DANGEROUS_PATTERNS, exclude_paths)
    if out["dangerous"]:
        out["errors"].append(f"发现 {len(out['dangerous'])} 处危险命令模式（#13）")

    out["scan_excluded_dirs"] = sorted(
        d for d in SCAN_EXEMPT_DIRS if (skill_dir / d).is_dir())

    # 依赖新鲜度（ADR-0009 延伸）：声明路径漂移 → warning（不 fail 闸门）
    out["stale_refs"] = find_stale_refs(skill_dir, body)
    for ref in out["stale_refs"]:
        out["warnings"].append(f"SKILL.md 声明引用的路径不存在：{ref}（依赖新鲜度）")

    # 可移植性闸门（ADR-0008）：宿主环境硬编码 → 整体 fail
    out["hardcoded"] = scan_patterns(skill_dir, HOST_HARDCODE_PATTERNS, exclude_paths)
    if out["hardcoded"]:
        out["errors"].append(
            f"发现 {len(out['hardcoded'])} 处宿主环境硬编码（可移植性闸门，ADR-0008）")

    out["passed"] = not out["errors"]
    out["issues"] = out["errors"] + out["warnings"]
    emit(out, out_path)


if __name__ == "__main__":
    main()
