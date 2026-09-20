"""静态检查（#2/#7/#13）。Seam: python static_check.py <skill目录> -> stdout JSON

stdout 契约（字段恒输出）：
{
  "name": str|null, "description": str|null, "description_tokens": int,
  "invoke": {"allowed": [str...], "resolved": str, "note": str},
  "dangerous": [{"pattern": str, "line": int, "file": str, "text": str}],
  "hardcoded": [{"pattern": str, "line": int, "file": str, "text": str}],   # 可移植性闸门（ADR-0008）
  "errors": [str], "warnings": [str], "issues": [str],   # issues = errors + warnings
  "passed": bool          # errors 为空即 True（危险命令/缺 name 都算 error）
}

局限：手写 frontmatter 解析只支持单行 key: value（不支持多行/嵌套列表），不要扩展。
"""
import json
import re
import sys
from pathlib import Path

# 可配置常量（reference.md § 可配参数）
DESCRIPTION_TOKEN_LIMIT = 100
NAME_MAX_CHARS = 64

INVOKE_VALUES = {"human", "agent", "both"}

DANGEROUS_PATTERNS = [
    r"rm\s+-rf?\b",
    r"\bdel\s+/[qs]",
    r"remove-item\s+.*-recurse",   # PowerShell
    r"git\s+push\s+(-f|--force)",
    r"git\s+reset\s+--hard",
    r"git\s+clean\s+-[a-z]*f",
    r"\bsudo\b",
    r"curl\s+[^|]*\|\s*(sudo\s+)?(ba)?sh",
    r"chmod\s+777",
    r"\bformat\b\s+[a-zA-Z]:",
    r"drop\s+table\b",
]

# 可移植性闸门（ADR-0008）：宿主环境硬编码 → 整体 fail。
# 只查硬编码，不查"声明给某 agent 用"（#7 调用方式与通用性无关）。
# 排除：相对路径与环境变量引用本就不匹配这些模式。
HOST_HARDCODE_PATTERNS = [
    r"[A-Za-z]:[\\/](?:Users|home)[\\/]",   # Windows 绝对用户路径（C:\Users\xxx、C:/home/xxx）
    r"(?<![\w.])/(?:Users|home)/[\w.-]+",   # POSIX 绝对用户目录（/Users/xxx、/home/xxx）
    r"(?<![\w.])\.(?:claude|cursor|codex)\b",  # 他方 agent 生态目录（.claude/.cursor/.codex）
    r"(?<![\w.])~[\\/]\.(?:claude|cursor|codex)\b",  # ~ 下的他方生态目录
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


def scan_patterns(skill_dir: Path, patterns, exclude_paths=None):
    hits = []
    for f in sorted(skill_dir.rglob("*")):
        if not f.is_file() or f.suffix not in (".py", ".sh", ".ps1", ".cmd", ".bat", ".js", ".md", ".json", ".toml", ".yaml", ".yml"):
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


def main():
    argv = sys.argv[1:]
    excludes = []
    i = 0
    rest = []
    while i < len(argv):
        if argv[i] == "--exclude":
            excludes.append(argv[i + 1]); i += 2
        else:
            rest.append(argv[i]); i += 1
    if len(rest) != 1:
        print("usage: static_check.py <skill目录> [--exclude <文件>]...", file=sys.stderr)
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
        print(json.dumps(out, ensure_ascii=False))
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

    # #7 调用方式：读 frontmatter invoke，缺省 both，非法值记 error
    raw_invoke = meta.get("invoke")
    if raw_invoke:
        if raw_invoke in INVOKE_VALUES:
            out["invoke"]["resolved"] = raw_invoke
        else:
            out["errors"].append(
                f"invoke 取值 '{raw_invoke}' 不在 {sorted(INVOKE_VALUES)}（#7）")

    out["dangerous"] = scan_patterns(skill_dir, DANGEROUS_PATTERNS, exclude_paths)
    if out["dangerous"]:
        out["errors"].append(f"发现 {len(out['dangerous'])} 处危险命令模式（#13）")

    # 可移植性闸门（ADR-0008）：宿主环境硬编码 → 整体 fail
    out["hardcoded"] = scan_patterns(skill_dir, HOST_HARDCODE_PATTERNS, exclude_paths)
    if out["hardcoded"]:
        out["errors"].append(
            f"发现 {len(out['hardcoded'])} 处宿主环境硬编码（可移植性闸门，ADR-0008）")

    out["passed"] = not out["errors"]
    out["issues"] = out["errors"] + out["warnings"]
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
