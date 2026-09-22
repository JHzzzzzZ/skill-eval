"""静态检查（#2/#7/#13）。Seam: python static_check.py <skill目录> -> stdout JSON

stdout 契约（字段恒输出）：
{
  "name": str|null, "description": str|null, "description_tokens": int,
  "invoke": {"allowed": [str...], "resolved": str, "note": str},
  "dangerous": [{"pattern": str, "line": int, "file": str, "text": str}],
  "secrets": [...], "injection": [...], "exfil": [...],   # 同为 #13，命中即 error（ADR-0013）
  "obfuscation": [...],                                # 同为 #13，命中仅 warning
  # 每行最多计一条（多规则同时命中取第一条）；ignored/errors 里的“处数”= 命中行数
  "hardcoded": [{"pattern": str, "line": int, "file": str, "text": str}],   # 可移植性闸门（ADR-0008）
  "excluded": [str],     # 实际未扫描的路径（相对 skill 目录；ADR-0012）
  "ignored": [{"pattern": str, "line": int, "file": str, "text": str}],   # 行内标记抑制掉的命中
  "errors": [str], "warnings": [str], "issues": [str],   # issues = errors + warnings
  "passed": bool          # errors 为空即 True（危险命令/缺 name 都算 error）
}

#13 = 五组规则（ADR-0013）：dangerous/secrets/injection/exfil 命中 → error；obfuscation 命中 → warning。
secrets 组带占位符过滤（your/xxx/example… 所在行不算硬编码凭据）。error 文案里不得出现
name/description/invoke 字样，否则 report.py 会误判成 #2/#7 的 fail 依据。

排除口径（ADR-0012）：
- 默认排除 tests/、evalsets/（测试夹具与历史 trace 按构造含危险命令）与扫描器自身源码（正则表自命中）
- --exclude 相对路径按 **skill 目录**解析（不是 CWD），绝对路径原样；--no-default-excludes 关闭默认排除
- 行内含 `static-check:ignore` 的命中不计入 errors，改记 `ignored`（文档里合法引用危险模式时用）

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

# 行内抑制标记：合法文档里引用危险模式（如本仓 reference.md 的硬编码说明行）时用
IGNORE_MARKER = "static-check:ignore"
# 默认排除的目录名：测试夹具/评测痕迹按构造满是危险命令字符串，扫它们只是自噪声
DEFAULT_EXCLUDE_DIRS = ("tests", "evalsets")

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

# 硬编码凭据（#13，命中即 error）。纯静态正则只拦常见形态（云厂商前缀/PEM/JWT + 高熵赋值）；
# 自造格式的高熵 token 认不出——这个局限写在 reference.md § 权限边界，不在这里堆启发式。
SECRETS_PATTERNS = [
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"\bAKIA[0-9A-Z]{16}\b",                                             # AWS access key id
    r"\b(?:sk|rk)-[A-Za-z0-9]{20,}\b",                                    # OpenAI/Stripe 风格前缀
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",                                   # GitHub token
    r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b",                                 # Slack token
    r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b",   # JWT
    r"\b(?:api[_-]?key|secret|token|passwd|password|access[_-]?key)\b\s*[:=]\s*['\"][^'\"]{16,}['\"]",
]

# 占位符过滤（仅 secrets 组）：API_KEY = "your-key-xxxx" 这类示例不算泄漏
PLACEHOLDER_RE = re.compile(
    r"(?i)\b(?:your|my|example|placeholder|dummy|sample|fake|redacted|replace|change|insert|todo|none|null)\b"
    r"|x{6,}"
)

# 注入类指令（#13，命中即 error）：不需任何 shell 权限，正文就能让 agent 越权
INJECTION_PATTERNS = [
    r"\bignore\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above|preceding)\s+(?:instruction|instructions|prompt|prompts|rules)",
    r"\bdisregard\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above)\b",
    r"\bdo\s+not\s+(?:tell|inform|notify|mention\s+(?:this\s+)?to)\s+the\s+user\b",
    r"\bwithout\s+(?:telling|informing|notifying)\s+the\s+user\b",
    r"\b(?:hide|conceal)\s+(?:this|these|it)\s+from\s+the\s+user\b",
    r"(?:不要|无需|不用)(?:告诉|告知|通知|提醒)(?:用户|使用者)",
    r"(?:对|向)用户(?:隐瞒|保密)",
    r"^\s*\[?(?:system|assistant)\]?\s*[:：]\s*(?:you\s+must|you\s+shall|忽略)",
    r"<\s*(?:system|important)\s*>",
    r"\bexfiltrat\w*\b",
    r"[\u202a-\u202e\u2066-\u2069\u200b-\u200d]",   # 双向控制/零宽字符：藏不可见指令的手法
]

# 数据外发与凭据读取（#13，命中即 error）
EXFIL_PATTERNS = [
    r"\bcurl\b.*(?:-d\s|--data|--data-binary|--data-raw|-F\s|--form|-T\s|--upload-file)",
    r"\bcurl\b.*\s-X\s*(?:POST|PUT|PATCH|DELETE)\b",
    r"\bwget\b.*--post",
    r"\b(?:requests|httpx)\.(?:post|put|patch)\s*\(",
    r"\burllib\.request\.(?:urlopen|Request)\b",
    r"\b(?:nc|ncat|netcat)\b.*\s-e\b|/dev/tcp/",
    r"(?:\.ssh/|id_rsa|id_ed25519|\.aws/credentials|\.netrc|\.git-credentials)",
    r"(?:cat|less|more|source|read_text|readlines|open)\s*\(?[^\n]{0,40}\.env\b",
    r"\.env\b[^\n]{0,40}\|\s*(?:curl|nc|mail|wget)",
    r"process\.env\b[^\n]{0,60}https?://",
]

# 混淆（#13，命中仅 warning）：混淆本身不是铁证（可能只是内嵌数据），交人工复核
OBFUSCATION_PATTERNS = [
    r"\bb64decode\b|\bbase64\s+(?:-d|--decode)\b|\batob\b",
    r"\beval\s*\(|\bexec\s*\(",
    r"\\x[0-9a-fA-F]{2}(?:\\x[0-9a-fA-F]{2}){7,}",        # 8 个以上连续 \xNN
    r"['\"][A-Za-z0-9+/]{120,}={0,2}['\"]",               # 120+ 字符疑似 base64 blob（sha256 hex=64 不触发）
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


def scan_patterns(skill_dir: Path, patterns, exclude_paths=None, ignored=None, skip_placeholders=False):
    excluded = list(exclude_paths or [])
    hits = []
    for f in sorted(skill_dir.rglob("*")):
        if not f.is_file() or f.suffix not in (".py", ".sh", ".ps1", ".cmd", ".bat", ".js", ".md", ".json", ".toml", ".yaml", ".yml"):
            continue
        if any(f.resolve().is_relative_to(e) for e in excluded):
            continue
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if skip_placeholders and PLACEHOLDER_RE.search(line):
                continue
            silenced = IGNORE_MARKER in line
            for p in patterns:
                if re.search(p, line, re.IGNORECASE):
                    entry = {"pattern": p, "line": i, "file": f.name, "text": line.strip()}
                    if silenced:
                        if ignored is not None:
                            ignored.append(entry)
                    else:
                        hits.append(entry)
                    break   # 一行只记一条：多规则同时命中时取第一条，避免"处数"虚高
    return hits


def resolve_excludes(skill_dir: Path, raw):
    """--exclude 的相对路径按 skill 目录解析（早期版本按 CWD 解析，排除会静默失效）。"""
    out = []
    for e in raw:
        p = Path(e)
        out.append((p if p.is_absolute() else skill_dir / p).resolve())
    return out


def default_excludes(skill_dir: Path):
    """默认排除：夹具/痕迹目录 + 扫描器自身源码（其正则表定义行按构造必然自命中）。"""
    paths = [d.resolve() for d in skill_dir.rglob("*") if d.is_dir() and d.name in DEFAULT_EXCLUDE_DIRS]
    own = (skill_dir / "scripts" / Path(__file__).name)
    if own.is_file():
        paths.append(own.resolve())
    return paths


def rel_paths(paths, skill_dir: Path):
    base = skill_dir.resolve()
    out = []
    for p in paths:
        try:
            out.append(p.relative_to(base).as_posix())
        except ValueError:
            out.append(str(p))
    return sorted(set(out))


def main():
    argv = sys.argv[1:]
    excludes = []
    no_defaults = False
    i = 0
    rest = []
    while i < len(argv):
        if argv[i] == "--exclude":
            excludes.append(argv[i + 1]); i += 2
        elif argv[i] == "--no-default-excludes":
            no_defaults = True; i += 1
        else:
            rest.append(argv[i]); i += 1
    if len(rest) != 1:
        print("usage: static_check.py <skill目录> [--exclude <文件|目录>]... [--no-default-excludes]", file=sys.stderr)
        sys.exit(2)
    skill_dir = Path(rest[0])
    exclude_paths = ([] if no_defaults else default_excludes(skill_dir)) + resolve_excludes(skill_dir, excludes)
    out = {"name": None, "description": None, "description_tokens": 0,
           "invoke": {"allowed": sorted(INVOKE_VALUES), "resolved": "both", "note": ""},
           "dangerous": [], "secrets": [], "injection": [], "exfil": [], "obfuscation": [],
           "hardcoded": [], "excluded": rel_paths(exclude_paths, skill_dir),
           "ignored": [], "errors": [], "warnings": [], "issues": [], "passed": True}
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

    out["dangerous"] = scan_patterns(skill_dir, DANGEROUS_PATTERNS, exclude_paths, out["ignored"])
    if out["dangerous"]:
        out["errors"].append(f"发现 {len(out['dangerous'])} 处危险命令模式（#13）")

    # #13 静态面其余四组（ADR-0013）。新 error 文案不得含 name/description/invoke 字样：
    # report.py 的历史实现用子串匹配把 errors 分流到 #2/#7（见 from_static）。
    out["secrets"] = scan_patterns(skill_dir, SECRETS_PATTERNS, exclude_paths, out["ignored"],
                                   skip_placeholders=True)
    if out["secrets"]:
        out["errors"].append(f"发现 {len(out['secrets'])} 处硬编码凭据（#13）")

    out["injection"] = scan_patterns(skill_dir, INJECTION_PATTERNS, exclude_paths, out["ignored"])
    if out["injection"]:
        out["errors"].append(f"发现 {len(out['injection'])} 处注入类指令（#13）")

    out["exfil"] = scan_patterns(skill_dir, EXFIL_PATTERNS, exclude_paths, out["ignored"])
    if out["exfil"]:
        out["errors"].append(f"发现 {len(out['exfil'])} 处数据外发或凭据读取（#13）")

    out["obfuscation"] = scan_patterns(skill_dir, OBFUSCATION_PATTERNS, exclude_paths, out["ignored"])
    if out["obfuscation"]:
        out["warnings"].append(f"发现 {len(out['obfuscation'])} 处混淆痕迹，需人工复核（#13，仅警告）")

    # 可移植性闸门（ADR-0008）：宿主环境硬编码 → 整体 fail
    out["hardcoded"] = scan_patterns(skill_dir, HOST_HARDCODE_PATTERNS, exclude_paths, out["ignored"])
    if out["hardcoded"]:
        out["errors"].append(
            f"发现 {len(out['hardcoded'])} 处宿主环境硬编码（可移植性闸门，ADR-0008）")

    out["passed"] = not out["errors"]
    out["issues"] = out["errors"] + out["warnings"]
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
