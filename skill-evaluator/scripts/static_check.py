"""静态检查（#1/#3/#4）。Seam: python static_check.py <skill目录> [--exclude <文件>] [--out <file>] -> stdout JSON

--out：脚本自写 UTF-8 文件（stdout 同步回显）——Windows 控制台重定向会产 GBK，
中间产物一律用 --out，禁止 shell 重定向（ADR-0009 同源原则）。

stdout 契约（字段恒输出）：
{
  "name": str|null, "description": str|null,
  "name_chars": int, "name_max_chars": int,
  "description_chars": int, "description_warn_chars": int, "description_fail_chars": int,
  "description_level": "ok"|"warn"|"fail"|"skipped", "human": bool,
  "invoke": {"allowed": [str...], "resolved": str, "note": str},   # resolved: both|human（映射 disable-model-invocation）
  "dangerous": [{"pattern": str, "line": int, "file": str, "text": str}],
  "secrets": [...], "injection": [...], "exfil": [...],   # 同为 #4，命中即 error（ADR-0013）
  "obfuscation": [...],                                # 同为 #4，命中仅 warning
  "egress": [...],                                     # 出站网络（GET/内网/ssh 等），仅 warning
  "pii": [{"pattern": str, "label": str, "line": int, "file": str, "text": str}],
                                                        # 同为 #4 的第六组（ADR-0029），命中仅 warning
  # 每行最多计一条（多规则同时命中取第一条）；ignored/errors 里的“处数”= 命中行数
  "hardcoded": [{"pattern": str, "line": int, "file": str, "text": str}],   # 可移植性闸门（ADR-0008）
  "deps_used": [str],     # 脚本实际用到的外部工具/包（shebang / import / subprocess argv[0]）
  "deps_declared": [str], # 其中已在 SKILL.md/reference.md/requirements.txt/check-deps.sh 出现过的
  "deps_missing": [str],  # 差集 → report.py 把 #12 至少降到 warn（ADR-0031）；宿主基座不计
  "excluded": [str],     # 实际未扫描的路径（相对 skill 目录；ADR-0012）
  "ignored": [{"pattern": str, "line": int, "file": str, "text": str}],   # 行内标记抑制掉的命中
  "scan_excluded_dirs": [str],   # 默认豁免目录中实际存在者（ADR-0012：运行面之外）
  "stale_refs": [str],           # SKILL.md 声明但包内不存在的路径（依赖新鲜度，warning 级）
  "skill_md_body_lines": int|null,       # #2 正文行数（frontmatter 之后全文；无 SKILL.md 时为 null）
  "skill_md_body_warn_lines": int, "skill_md_body_fail_lines": int,
  "body_level": "ok"|"warn"|"fail",
  "errors": [str], "warnings": [str], "issues": [str],   # issues = errors + warnings
  "passed": bool          # errors 为空即 True（危险命令/缺 name 都算 error）
}

#4 = 六组规则 + 出站网络（ADR-0013/0029）：dangerous/secrets/injection/exfil 命中 → error；
obfuscation/pii/egress 命中 → warning。secrets 组带占位符过滤（your/xxx/example… 所在行不算硬编码凭据）。
error 文案里不得出现 name/description/invoke 字样，否则 report.py 会误判成 #1/#3 的 fail 依据。

排除口径（ADR-0012：机制 + 作用域）：
- 默认排除 tests/、evalsets/、uploads/、.sandbox/（测试夹具、历史 trace、只读存档都不随 skill 运行）
  与扫描器自身源码（正则表自命中）
- --exclude 相对路径按 **skill 目录**解析（不是 CWD），绝对路径原样；--no-default-excludes 关闭默认排除
- 行内含 `static-check:ignore` 的命中不计入 errors，改记 `ignored`（文档里合法引用危险模式时用）

局限：手写 frontmatter 解析只支持单行 key: value（不支持多行/嵌套列表），不要扩展。
"""
import _console

_console.fix()

import datetime
import json
import os
import re
import sys
from pathlib import Path

import _config

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

# 硬编码凭据（#4，命中即 error）。纯静态正则只拦常见形态（云厂商前缀/PEM/JWT + 高熵赋值）；
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

# 注入类指令（#4，命中即 error）：不需任何 shell 权限，正文就能让 agent 越权
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

# 数据外发与凭据读取（#4，命中即 error）
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

# 出站网络调用（#4，命中仅 warning）：POST 只是其中一种；GET 查询串、内网、socket、ssh 都算。
# 与 EXFIL 的区别：这里只证明“会发网络请求”，不证明“发了敏感数据”，所以不 fail。
EGRESS_PATTERNS = [
    r"\bcurl\b", r"\bwget\b",
    r"\b(?:requests|httpx)\.(?:get|post|put|patch|delete|head|options|request)\s*\(",
    r"\burllib\.request\b", r"\bhttp\.client\b", r"\bfetch\s*\(",
    r"\bsocket\.", r"\b(?:nc|ncat|netcat)\b", r"\bssh\b", r"\bscp\b",
    r"\brsync\b", r"\bftp\b", r"\bInvoke-WebRequest\b", r"\bInvoke-RestMethod\b",
    r"\bwebhook\b",
]

# 混淆（#4，命中仅 warning）：混淆本身不是铁证（可能只是内嵌数据），交人工复核
OBFUSCATION_PATTERNS = [
    r"\bb64decode\b|\bbase64\s+(?:-d|--decode)\b|\batob\b",
    r"\beval\s*\(|\bexec\s*\(",
    r"\\x[0-9a-fA-F]{2}(?:\\x[0-9a-fA-F]{2}){7,}",        # 8 个以上连续 \xNN
    r"['\"][A-Za-z0-9+/]{120,}={0,2}['\"]",               # 120+ 字符疑似 base64 blob（sha256 hex=64 不触发）
]

# 个人信息（#4，第六组，ADR-0029）：命中仅 warning——文档里写示例联系方式是合法的，
# 是不是真数据需人工判断。只认**可格式化**的个人标识；工号/客户名/内部系统地址这类
# 公司特化信息没有可枚举格式，交给 judges/pii.md 语义面。
PII_EMAIL = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
PII_MOBILE = r"(?<!\d)1[3-9]\d{9}(?!\d)"
PII_ID_CARD = (r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
               r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)")
PII_BANK_CARD = r"(?<!\d)\d{16,19}(?!\d)"
PII_IPV4 = r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"
PII_PATTERNS = [("邮箱", PII_EMAIL), ("手机号", PII_MOBILE), ("身份证", PII_ID_CARD),
                ("银行卡", PII_BANK_CARD), ("IP", PII_IPV4)]

# 示例值白名单（只看命中到的 token）：标准示例域名/保留号段/GB 11643 标准示例证件号/常见测试卡号
PII_WHITELIST_RE = re.compile(
    r"(?i)example\.(?:com|org|net)|localhost|test\.com|13800138000|11010519491231002X|4111111111111111")
# 行级占位符过滤（与 secrets 组同口径）：中文文档写“示例/占位”很常见，
# 英文占位词与 secrets 组共用同一套（PLACEHOLDER_RE 的 pattern 直接内联，便于单处改）。
PII_PLACEHOLDER_RE = re.compile(
    PLACEHOLDER_RE.pattern + r"|示例|样例|占位|虚构|测试用|举例|例如|假的")

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"
_PRIVATE_IP_RE = re.compile(
    r"^(?:0|10|127|169\.254|172\.(?:1[6-9]|2\d|3[01])|192\.168|192\.0\.2|198\.51\.100"
    r"|203\.0\.113|22[4-9]|23\d|24\d|25[0-5])\.")


def _luhn_ok(s: str) -> bool:
    """银行卡 Luhn 校验：挡掉任意 16–19 位数字串（如时间戳拼接、哈希片段）。"""
    digits = [int(c) for c in s]
    if not 16 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _cn_id_ok(s: str) -> bool:
    """18 位身份证：出生日期合法 + ISO 7064 MOD 11-2 校验位（挡掉纯随机 18 位数字）。"""
    s = s.upper()
    if not s[:17].isdigit() or not (s[17].isdigit() or s[17] == "X"):
        return False
    try:
        datetime.date(int(s[6:10]), int(s[10:12]), int(s[12:14]))
    except ValueError:
        return False
    return _ID_CHECK[sum(int(c) * w for c, w in zip(s[:17], _ID_WEIGHTS)) % 11] == s[17]


def _public_ip_ok(s: str) -> bool:
    """公网 IPv4：排除私网/回环/链路本地/文档段/组播段（内部地址是部署事实，不是个人信息）。"""
    parts = s.split(".")
    if len(parts) != 4 or any(not p.isdigit() or int(p) > 255 for p in parts):
        return False
    return not _PRIVATE_IP_RE.match(s)


PII_VALIDATORS = {PII_BANK_CARD: _luhn_ok, PII_ID_CARD: _cn_id_ok, PII_IPV4: _public_ip_ok}


def _pii_accept(m, pattern) -> bool:
    """先过**行级**占位符/示例过滤（与 secrets 组同口径），再过格式校验。

    行级而非 token 级：`示例：zhangsan@corp.com` 这类说明句里 token 本身不带占位词，
    只看 token 会误报（评审 P2）。
    """
    if PII_PLACEHOLDER_RE.search(m.string) or PII_WHITELIST_RE.search(m.group(0)):
        return False
    check = PII_VALIDATORS.get(pattern)
    return check(m.group(0)) if check else True


# 可移植性闸门（ADR-0008）：宿主环境硬编码 → 整体 fail。
# 只查硬编码，不查"声明给某 agent 用"（#3 调用方式与通用性无关）。
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


# 依赖全面性（ADR-0031）：脚本实际用了什么 vs 文档声明了什么。
# 只扫运行面（ADR-0012 同源），声明面取四者并集：SKILL.md 正文、reference.md、
# requirements.txt、check-deps.sh——单一来源都会漏（requirements 只覆盖 python 包，
# check-deps.sh 只覆盖硬依赖，只有正文会写“需要 pi CLI”）。
DECLARATION_FILES = ("SKILL.md", "reference.md", "requirements.txt", "check-deps.sh")
# 宿主基座：由操作系统与 pi 运行时提供，未声明也不报（否则每个 skill 都缺 python/bash）
BASELINE_DEPS = frozenset({"python", "sh", "bash", "zsh", "env", "cmd", "powershell", "pwsh"})
def _shebang_tool(text: str):
    """从 shebang 取解释器名：`#!/usr/bin/env python3` → python3；`#!/bin/bash` → bash。

    不用单个正则：懒匹配路径会抓到 `bin`（实测 deps_used 里出现过 "bin"）。
    """
    if not text.startswith("#!"):
        return None
    toks = text.splitlines()[0][2:].strip().split() if text.splitlines() else []
    if not toks:
        return None
    if toks[0].rsplit("/", 1)[-1] == "env":
        toks = [t for t in toks[1:] if not t.startswith("-")]   # 跳过 `env -S python3 -u` 里的选项
    return (toks[0].rsplit("/", 1)[-1] or None) if toks else None


IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]*)", re.M)
SUBPROCESS_RE = re.compile(r"subprocess\.(?:run|Popen|check_call|check_output)\s*\(\s*\[([^\]]*)\]")
ARGV0_RE = re.compile(r"['\"]([^'\"]+)['\"]")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_+-]*$")   # 拒绝 ./run.sh、x.py 这类路径
_STDLIB_FALLBACK = frozenset(
    ("argparse base64 collections concurrent contextlib csv datetime functools glob hashlib http "
     "inspect io itertools json logging math os pathlib random re shutil socket statistics "
     "subprocess sys tempfile textwrap threading time traceback typing unicodedata urllib "
     "uuid warnings zipfile").split())


def _norm_tool(name: str) -> str:
    """工具名归一：去引号/后缀/版本号（python3.11 → python）。"""
    n = name.strip().strip("\"'").lower()
    for suf in (".exe", ".cmd", ".bat", ".ps1", ".sh"):
        if n.endswith(suf):
            n = n[: -len(suf)]
    m = re.fullmatch(r"(python|node|ruby|perl|php)\d+(?:\.\d+)*", n)
    return m.group(1) if m else n


def _stdlib_names():
    names = getattr(sys, "stdlib_module_names", None)   # 3.10+；3.9 走兜底表
    return frozenset(names) if names else _STDLIB_FALLBACK


def collect_deps(skill_dir: Path, exclude_paths):
    """返回 (used, declared, missing)：used − declared − 宿主基座 = missing（ADR-0031）。

    已知局限：subprocess 的 argv 由变量拼时取不到 argv[0]（如 [sys.executable, ...]），
    声明面靠子串匹配（“Python3” 与 “python” 这类同义词会漏判）——宁可漏报，不造假 fail。
    """
    std = _stdlib_names()
    used = set()
    for d, files in walk_tree(skill_dir, list(exclude_paths)):
        for name in sorted(files):
            f = d / name
            if f.suffix not in SCAN_SUFFIXES:
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            tool = _shebang_tool(text)
            if tool:
                used.add(_norm_tool(tool))
            for mod in IMPORT_RE.findall(text):
                top = mod.split(".")[0]
                if top in std or (f.parent / f"{top}.py").is_file() \
                        or (skill_dir / f"{top}.py").is_file():
                    continue          # 标准库与包内本地模块不算外部依赖
                used.add(_norm_tool(top))
            for argv in SUBPROCESS_RE.findall(text):
                first = ARGV0_RE.search(argv)
                if first and _TOOL_NAME_RE.match(first.group(1).strip()):
                    used.add(_norm_tool(first.group(1)))
    declared_names = set()
    for name in DECLARATION_FILES:
        p = skill_dir / name
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if name == "requirements.txt":
            declared_names |= {_norm_tool(re.split(r"[<>=!~;\[\s]", ln, 1)[0])
                               for ln in text.splitlines() if ln.strip()}
        declared_names |= set(re.findall(r"[A-Za-z_][\w.+-]*", text))
    declared = sorted(t for t in used if t in declared_names)
    missing = sorted(used - set(declared) - BASELINE_DEPS)
    return sorted(used), declared, missing


def parse_frontmatter(text: str):
    """解析单行 key: value + YAML 块标量（| / >），支持多行 description。

    不引入 PyYAML：只覆盖 frontmatter 常见写法。块标量要求缩进 2 空格或 1 tab。
    """
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}, text
    meta = {}
    lines = m.group(1).splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if value in ("|", "|-", ">", ">-"):
            block = []
            i += 1
            while i < len(lines) and (lines[i].startswith("  ") or lines[i].startswith("\t")):
                block.append(lines[i][2:] if lines[i].startswith("  ") else lines[i][1:])
                i += 1
            meta[key] = "\n".join(block) if value.startswith("|") else " ".join(x.strip() for x in block)
            continue
        if value[:1] in ("'", '"'):
            quote = value[0]
            val = value[1:]
            while not val.endswith(quote) and i + 1 < len(lines):
                i += 1
                val += "\n" + lines[i]
            val = val.rstrip(quote).replace("\n", "\n")
            meta[key] = val.strip()
        else:
            meta[key] = value.strip().strip('"')
        i += 1
    return meta, text[m.end():]


# 依赖新鲜度（ADR-0009 延伸）：SKILL.md 声明的路径引用应能定位到包内文件。
# 只查“声明了但不存在”（文档漂移），不查语义对错——那是 #12 语义面（judges/deps.md）的事。
REF_EXTS = ".py .mjs .js .ts .md .json .sh .ps1 .cmd .bat .toml .yaml .yml".split()


def find_stale_refs(skill_dir: Path, body: str):
    refs, seen = [], set()
    # 代码块里的路径是示例命令，不是声明引用；先剥掉，避免反引号跨行贪婪吞掉正文。
    body = re.sub(r"```.*?```", "", body, flags=re.S)

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


def scan_patterns(skill_dir: Path, patterns, exclude_paths=None, ignored=None, skip_placeholders=False,
                  accept=None):
    """逐行匹配模式表。patterns 项可以是 str，也可以是 (标签, 正则)——带标签的命中多一个 label 字段。

    accept(match, 正则) 可做逐命中二次校验（Luhn / 证件校验位 / 公网判断）；返回 False 即丢弃该命中。
    """
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
                    label, regex = p if isinstance(p, tuple) else (None, p)
                    m = re.search(regex, line, re.IGNORECASE)
                    if not m:
                        continue
                    if accept is not None and not accept(m, regex):
                        continue
                    try:
                        rel = f.relative_to(skill_dir).as_posix()
                    except ValueError:
                        rel = f.name
                    entry = {"pattern": regex, "line": i, "file": rel, "text": line.strip()}
                    if label:
                        entry["label"] = label
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
    name_max_chars = _config.get_int("static.name_max_chars", 64)
    desc_warn = _config.get_int("static.description_warn_chars", 384)
    desc_fail = _config.get_int("static.description_fail_chars", 1024)
    body_warn = _config.get_int("static.body_warn_lines", 150)
    body_fail = _config.get_int("static.body_fail_lines", 500)
    if desc_warn > desc_fail:
        raise _config.ConfigError("static.description_warn_chars 不能大于 description_fail_chars")
    if body_warn > body_fail:
        raise _config.ConfigError("static.body_warn_lines 不能大于 body_fail_lines")
    exclude_paths = ([] if no_defaults else default_excludes(skill_dir)) + resolve_excludes(skill_dir, excludes)
    out = {"name": None, "description": None,
           "name_chars": 0, "name_max_chars": name_max_chars,
           "description_chars": 0, "description_warn_chars": desc_warn,
           "description_fail_chars": desc_fail, "description_level": "skipped",
           "human": False,
           "invoke": {"allowed": sorted(INVOKE_VALUES), "resolved": "both", "note": ""},
           "dangerous": [], "secrets": [], "injection": [], "exfil": [], "obfuscation": [], "pii": [],
           "egress": [],
           "hardcoded": [], "excluded": rel_paths(exclude_paths, skill_dir),
           "ignored": [], "scan_excluded_dirs": [], "stale_refs": [],
           "deps_used": [], "deps_declared": [], "deps_missing": [],
           "skill_md_body_lines": None, "skill_md_body_warn_lines": body_warn,
           "skill_md_body_fail_lines": body_fail,
           "body_level": "ok",
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

    # #3 调用方式：映射 pi 真实 frontmatter 字段 disable-model-invocation（ADR-0008）。
    # human 只认这个显式字段（ADR-0037）：name/description 缺失可能是坏包，不能反推调用方式。
    raw = (meta.get("disable-model-invocation") or "").strip().strip('"').lower()
    invoke_human = raw in ("true", "yes", "1")
    human = invoke_human
    out["human"] = human

    if not out["name"]:
        if not human:
            out["errors"].append("frontmatter 缺 name")
    else:
        out["name_chars"] = len(out["name"])
        if out["name_chars"] > name_max_chars:
            out["warnings"].append(f"name {out['name_chars']} 字符，超过 {name_max_chars}（#1）")

    if not out["description"]:
        out["description_level"] = "skipped" if human else "fail"
        if not human:
            out["errors"].append("frontmatter 缺 description")
    else:
        out["description_chars"] = len(out["description"].strip())
        if out["description_chars"] > desc_fail:
            out["description_level"] = "fail"
            out["errors"].append(
                f"description {out['description_chars']} 字符，超过失败阈值 {desc_fail}（#1）")
        elif out["description_chars"] > desc_warn:
            out["description_level"] = "warn"
            out["warnings"].append(
                f"description {out['description_chars']} 字符，超过警告阈值 {desc_warn}（#1）")
        else:
            out["description_level"] = "ok"

    # #2 正文精简（ADR-0017）：行数属可数事实 → 静态面出数，report.py 拿它当主源。
    # 语义两项（无可下沉细节、有索引结构）仍由 judges/brevity.md 判，两边在报告里合成。
    out["skill_md_body_lines"] = len(body.splitlines())
    if out["skill_md_body_lines"] > body_fail:
        out["body_level"] = "fail"
        out["errors"].append(
            f"SKILL.md 正文 {out['skill_md_body_lines']} 行，超过失败阈值 {body_fail}（#2）")
    elif out["skill_md_body_lines"] > body_warn:
        out["body_level"] = "warn"
        out["warnings"].append(
            f"SKILL.md 正文 {out['skill_md_body_lines']} 行，超过警告阈值 {body_warn}（#2）")

    if invoke_human:
        out["invoke"]["resolved"] = "human"
        out["invoke"]["note"] = ("disable-model-invocation=true：对模型隐藏，仅 /skill:name 手动调用"
                                 "；name/description 可省略")
    elif raw in ("", "false", "0", "no"):
        out["invoke"]["note"] = "缺省/false：模型按 description 触发，用户也可手动调用"
    else:
        out["warnings"].append(
            f"disable-model-invocation 取值 '{raw}' 不是布尔（pi 将按未知字段忽略）（#3）")

    out["dangerous"] = scan_patterns(skill_dir, DANGEROUS_PATTERNS, exclude_paths, out["ignored"])
    if out["dangerous"]:
        out["errors"].append(f"发现 {len(out['dangerous'])} 处危险命令模式（#4）")

    # 默认豁免目录里实际存在者（ADR-0012：运行面之外），供报告与审计看见
    out["scan_excluded_dirs"] = sorted(
        d for d in DEFAULT_EXCLUDE_DIRS if (skill_dir / d).is_dir())

    # #4 静态面其余四组（ADR-0013）。新 error 文案不得含 name/description/invoke 字样：
    # report.py 的历史实现用子串匹配把 errors 分流到 #1/#3（见 from_static）。
    out["secrets"] = scan_patterns(skill_dir, SECRETS_PATTERNS, exclude_paths, out["ignored"],
                                   skip_placeholders=True)
    if out["secrets"]:
        out["errors"].append(f"发现 {len(out['secrets'])} 处硬编码凭据（#4）")

    out["injection"] = scan_patterns(skill_dir, INJECTION_PATTERNS, exclude_paths, out["ignored"])
    if out["injection"]:
        out["errors"].append(f"发现 {len(out['injection'])} 处注入类指令（#4）")

    out["exfil"] = scan_patterns(skill_dir, EXFIL_PATTERNS, exclude_paths, out["ignored"])
    if out["exfil"]:
        out["errors"].append(f"发现 {len(out['exfil'])} 处数据外发或凭据读取（#4）")

    # 出站网络调用：不区分公网/内网，GET/查询串也算；只 warning，交人工判断是否传敏感数据
    out["egress"] = scan_patterns(skill_dir, EGRESS_PATTERNS, exclude_paths, out["ignored"])
    if out["egress"]:
        out["warnings"].append(
            f"发现 {len(out['egress'])} 处出站网络调用（#4，仅警告：可能访问内网/外部服务）")

    out["obfuscation"] = scan_patterns(skill_dir, OBFUSCATION_PATTERNS, exclude_paths, out["ignored"])
    if out["obfuscation"]:
        out["warnings"].append(f"发现 {len(out['obfuscation'])} 处混淆痕迹，需人工复核（#4，仅警告）")

    # #4 第六组：个人信息（ADR-0029）。只判 warning；公司特化标识（工号/客户名/内部系统）
    # 不在静态面能力范围内，走 judges/pii.md 语义面。
    out["pii"] = scan_patterns(skill_dir, PII_PATTERNS, exclude_paths, out["ignored"],
                               accept=_pii_accept)
    if out["pii"]:
        out["warnings"].append(
            f"发现 {len(out['pii'])} 处疑似个人信息，需人工复核是否已脱敏（#4，仅警告）")

    # 依赖全面性（ADR-0031）：用了什么 vs 声明了什么 → report.py 把 #12 至少降到 warn
    out["deps_used"], out["deps_declared"], out["deps_missing"] = collect_deps(skill_dir, exclude_paths)
    if out["deps_missing"]:
        out["warnings"].append(
            f"依赖未声明：{'、'.join(out['deps_missing'])}（#12 依赖全面性，ADR-0031，仅警告）")

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
