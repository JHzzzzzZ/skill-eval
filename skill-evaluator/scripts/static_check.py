"""静态检查（#2/#7/#13）。Seam: python static_check.py <skill目录> [--exclude <文件>] [--out <file>] -> stdout JSON

--out：脚本自写 UTF-8 文件（stdout 同步回显）——Windows 控制台重定向会产 GBK，
中间产物一律用 --out，禁止 shell 重定向（ADR-0009 同源原则）。

stdout 契约（字段恒输出）：
{
  "name": str|null, "description": str|null, "description_tokens": int,
  "invoke": {"allowed": [str...], "resolved": str, "note": str},   # resolved: both|human（映射 disable-model-invocation）
  "dangerous": [{"pattern": str, "line": int, "file": str, "text": str}],
  "secrets": [...], "injection": [...], "exfil": [...],   # 同为 #13，命中即 error（ADR-0013）
  "obfuscation": [...],                                # 同为 #13，命中仅 warning
  # 每行最多计一条（多规则同时命中取第一条）；ignored/errors 里的“处数”= 命中行数
  "hardcoded": [{"pattern": str, "line": int, "file": str, "text": str}],   # 可移植性闸门（ADR-0008）
  "excluded": [str],     # 实际未扫描的路径（相对 skill 目录；ADR-0012）
  "ignored": [{"pattern": str, "line": int, "file": str, "text": str}],   # 行内标记抑制掉的命中
  "scan_excluded_dirs": [str],   # 默认豁免目录中实际存在者（ADR-0012：运行面之外）
  "stale_refs": [str],           # SKILL.md 声明但包内不存在的路径（依赖新鲜度，warning 级）
  "skill_md_body_lines": int|null,       # #3 正文行数（frontmatter 之后全文；无 SKILL.md 时为 null）
  "skill_md_body_max_lines": int,        # 该次检查使用的阈值（自描述，report.py 据此判定，不复制常量）
  "errors": [str], "warnings": [str], "issues": [str],   # issues = errors + warnings
  "passed": bool          # errors 为空即 True（危险命令/缺 name 都算 error）
}

#13 = 五组规则（ADR-0013）：dangerous/secrets/injection/exfil 命中 → error；obfuscation 命中 → warning。
secrets 组带占位符过滤（your/xxx/example… 所在行不算硬编码凭据）。error 文案里不得出现
name/description/invoke 字样，否则 report.py 会误判成 #2/#7 的 fail 依据。

排除口径（ADR-0012：机制 + 作用域）：
- 默认排除 tests/、evalsets/、uploads/、.sandbox/（测试夹具、历史 trace、只读存档都不随 skill 运行）
  与扫描器自身源码（正则表自命中）
- --exclude 相对路径按 **skill 目录**解析（不是 CWD），绝对路径原样；--no-default-excludes 关闭默认排除
- 行内含 `static-check:ignore` 的命中不计入 errors，改记 `ignored`（文档里合法引用危险模式时用）

局限：手写 frontmatter 解析只支持单行 key: value（不支持多行/嵌套列表），不要扩展。
"""
import _console

_console.fix()

import json
import os
import re
import sys
from pathlib import Path

# 可配置常量（reference.md § 可配参数）
DESCRIPTION_TOKEN_LIMIT = 100
NAME_MAX_CHARS = 64
# #3 正文行数上限（ADR-0017）：口径 = parse_frontmatter 之后的全文行数，含空行、含代码块。
# 这是自定阈值（原 rubric 的 ">150 行倾向臃肿" 的硬化），不是 19 条需求里的数字；超限仅 warning。
SKILL_MD_BODY_MAX_LINES = 150

INVOKE_VALUES = {"human", "both"}  # pi 真实三态归约：缺省/false=both（模型+用户）；disable-model-invocation:true=human

# 默认排除的目录名（ADR-0012：机制 + 作用域）：闸门只扫运行面。
# tests/ 是测试夹具（tmp_path 运行时路径、危险命令用例）；evalsets/uploads/.sandbox 是评估
# 产物与只读存档（trace、报告，含评估时机器路径）——都不随 skill 运行，扫它们只是自噪声。
# .skillrepos/ 是沙箱副本 repo（每个被测 skill 一份完整拷贝，含 scripts/static_check.py）：
# 不排除它，自检就会在自己副本的正则表定义行上自命中（实测 4 处 exfil → 237 个测试里
# test_evaluator_self_check_passes 挂）。副本是运行面痕迹，不是被测内容。
DEFAULT_EXCLUDE_DIRS = ("tests", "evalsets", "uploads", ".sandbox", ".skillrepos")

# 行内抑制标记：合法文档里引用危险模式（如本仓 reference.md 的硬编码说明行）时用
IGNORE_MARKER = "static-check:ignore"

# 扫描的文本后缀（单一事实来源，别再散在循环里）
SCAN_SUFFIXES = frozenset({".py", ".sh", ".ps1", ".cmd", ".bat", ".js", ".md", ".json", ".toml", ".yaml", ".yml"})
# 遍历时直接剪掉的目录：版本库/缓存不是 skill 运行面，且文件数极大（实测 .sandbox 3.2 万文件）
PRUNE_DIRS = frozenset({".git", ".pytest_cache", "__pycache__"})

DANGEROUS_PATTERNS = [
    r"rm\s+-rf?\b",
    r"\bdel\s+/[qs]",
    r"remove-item\s+.*-recurse",   # PowerShell
    r"git\s+push\s+(-f|--force)",
    r"git\s+reset\s+--hard",
    r"git\s+clean\s+-[a-z]*f",
    r"\b" + "su" + "do\b",   # 拼接避免模式表自匹配（表是数据不是行为；自排除见 ADR-0012）
    r"curl\s+[^|]*\|\s*(" + "su" + r"do\b\s+)?(ba)?sh",
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
# 排除：相对路径与环境变量引用本就不匹配这些模式。具体路径示例不写入本文件注释，
# 免得模式表自匹配（ADR-0012：表是数据不是行为）。
# 作用域（ADR-0012）：闸门只扫运行面——tests/ 内的夹具（tmp_path 运行时路径、
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


def walk_tree(root: Path, excluded=()):
    """单次遍历并剪枝：yield (目录, 文件名列表)；`excluded` 里的子树整棵跳过。

    不用 rglob("*")：rglob 会把被排除目录里的每个文件也 stat 一遍再过滤，排除等于白做。
    实测评估器目录含 .sandbox/（3.2 万文件）时，单次 rglob 14s、六组规则共 144s；
    改成遍历时剪枝后整体回到秒级。剪枝靠改写 dirnames 生效（os.walk 才会不再下降）。
    """
    excluded = list(excluded)
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        if any(d.resolve().is_relative_to(e) for e in excluded):
            dirnames[:] = []          # 整棵子树剪掉（否则仍会下降进 3.2 万文件）
            continue
        dirnames[:] = sorted(x for x in dirnames if x not in PRUNE_DIRS)
        yield d, filenames


def scan_patterns(skill_dir: Path, patterns, exclude_paths=None, ignored=None, skip_placeholders=False):
    excluded = list(exclude_paths or [])
    hits = []
    for d, files in walk_tree(skill_dir, excluded):
        for name in sorted(files):
            f = d / name
            if f.suffix not in SCAN_SUFFIXES:
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


def emit(out: dict, out_path) -> None:
    """stdout 同步回显；--out 存在时另写 UTF-8 文件（禁止 shell 重定向的 GBK 风险）。"""
    text = json.dumps(out, ensure_ascii=False)
    if out_path:
        _console.write_text(out_path, text)
    print(text)


def resolve_excludes(skill_dir: Path, raw):
    """--exclude 的相对路径按 skill 目录解析（早期版本按 CWD 解析，排除会静默失效）。"""
    out = []
    for e in raw:
        p = Path(e)
        out.append((p if p.is_absolute() else skill_dir / p).resolve())
    return out


def default_excludes(skill_dir: Path):
    """默认排除：夹具/痕迹目录 + 扫描器自身源码（其正则表定义行按构造必然自命中）。

    边走边剪：命中排除名的目录记下后**不再深入**——否则 .sandbox/ 这类目录里的
    每一个同名子目录都会被枚举（实测 3.2 万文件、单这一步 40s+），而它们本就在排除面内。
    """
    paths = []
    for dirpath, dirnames, _files in os.walk(skill_dir):
        keep = []
        for name in sorted(dirnames):
            d = Path(dirpath) / name
            if d != skill_dir and name in DEFAULT_EXCLUDE_DIRS:
                paths.append(d.resolve())
                continue
            if name not in PRUNE_DIRS:
                keep.append(name)
        dirnames[:] = keep
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
    out_path = None
    i = 0
    rest = []
    while i < len(argv):
        if argv[i] == "--exclude":
            excludes.append(argv[i + 1]); i += 2
        elif argv[i] == "--no-default-excludes":
            no_defaults = True; i += 1
        elif argv[i] == "--out":
            out_path = Path(argv[i + 1]); i += 2
        else:
            rest.append(argv[i]); i += 1
    if len(rest) != 1:
        print("usage: static_check.py <skill目录> [--exclude <文件|目录>]... "
              "[--no-default-excludes] [--out <file>]", file=sys.stderr)
        sys.exit(2)
    skill_dir = Path(rest[0])
    exclude_paths = ([] if no_defaults else default_excludes(skill_dir)) + resolve_excludes(skill_dir, excludes)
    out = {"name": None, "description": None, "description_tokens": 0,
           "invoke": {"allowed": sorted(INVOKE_VALUES), "resolved": "both", "note": ""},
           "dangerous": [], "secrets": [], "injection": [], "exfil": [], "obfuscation": [],
           "hardcoded": [], "excluded": rel_paths(exclude_paths, skill_dir),
           "ignored": [], "scan_excluded_dirs": [], "stale_refs": [],
           "skill_md_body_lines": None, "skill_md_body_max_lines": SKILL_MD_BODY_MAX_LINES,
           "errors": [], "warnings": [], "issues": [], "passed": True}
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

    # #3 正文精简（ADR-0017）：行数属可数事实 → 静态面出数，report.py 拿它当主源。
    # 语义两项（无可下沉细节、有索引结构）仍由 judges/brevity.md 判，两边在报告里合成。
    out["skill_md_body_lines"] = len(body.splitlines())
    if out["skill_md_body_lines"] > SKILL_MD_BODY_MAX_LINES:
        out["warnings"].append(
            f"SKILL.md 正文 {out['skill_md_body_lines']} 行，超过 {SKILL_MD_BODY_MAX_LINES}（#3，仅警告）")

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

    out["dangerous"] = scan_patterns(skill_dir, DANGEROUS_PATTERNS, exclude_paths, out["ignored"])
    if out["dangerous"]:
        out["errors"].append(f"发现 {len(out['dangerous'])} 处危险命令模式（#13）")

    # 默认豁免目录里实际存在者（ADR-0012：运行面之外），供报告与审计看见
    out["scan_excluded_dirs"] = sorted(
        d for d in DEFAULT_EXCLUDE_DIRS if (skill_dir / d).is_dir())

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

    # 依赖新鲜度（ADR-0009 延伸）：声明路径漂移 → warning（不 fail 闸门）
    out["stale_refs"] = find_stale_refs(skill_dir, body)
    for ref in out["stale_refs"]:
        out["warnings"].append(f"SKILL.md 声明引用的路径不存在：{ref}（依赖新鲜度）")

    # 可移植性闸门（ADR-0008）：宿主环境硬编码 → 整体 fail
    out["hardcoded"] = scan_patterns(skill_dir, HOST_HARDCODE_PATTERNS, exclude_paths, out["ignored"])
    if out["hardcoded"]:
        out["errors"].append(
            f"发现 {len(out['hardcoded'])} 处宿主环境硬编码（可移植性闸门，ADR-0008）")

    out["passed"] = not out["errors"]
    out["issues"] = out["errors"] + out["warnings"]
    emit(out, out_path)


if __name__ == "__main__":
    main()
