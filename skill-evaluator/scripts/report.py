"""报告生成（17/18 的自我应用：固定输出契约）。
Seam: python report.py <results目录> [--html] [--evalset <evalsets/<name>/vN>] [--skill <被测skill目录>]

读同目录中间产物（static.json/score.json/idem.json/compare.json/golden.json/evalset.json/
model_robust.json/judges/*.json），产出 report.json（16 key，机器可读；schema=2，ADR-0032）+ report.md（人可读）
+ meta.json（本次运行的元数据：评估器指纹 + 被测 skill 指纹 + 评测集来源，ADR-0010）。
契约见 tests/test_report.py 模块注释。
"""
import _console

_console.fix()

import hashlib
import json
import re
import statistics
import sys
from pathlib import Path

import _config
META_SCHEMA = "skill-eval/results-meta/1"  # 与 uploads/、evalsets/ 下的同名 meta.json 靠此字段区分
EVALUATOR_ROOT = Path(__file__).resolve().parent.parent  # 评估器自身目录（指纹范围）

# 每个指标的中文含义（report.md 与 report.json 的 note 均引用，不能只写 #N）；编号见 ADR-0032。
DIM_NAMES = {
    "#1": "name/description 规范与字符预算", "#2": "正文精简",
    "#3": "调用方式", "#4": "权限最小化",
    "#5": "低冗余", "#6": "fallback", "#7": "前置自检",
    "#8": "输入契约", "#9": "输出契约",
    "#10": "触发精准度",
    "#11": "必要性 A/B", "#12": "最小依赖",
    "#13": "运行成本", "#14": "结果可验证", "#15": "稳定性", "#16": "幂等可恢复",
}

# #4 的六个静态规则组（ADR-0013/0029）：用于 note 与表格里的命中摘要
DIM_PERM_GROUPS = {"dangerous": "危险命令", "secrets": "硬编码凭据",
                   "injection": "注入指令", "exfil": "数据外发", "obfuscation": "混淆",
                   "pii": "个人信息"}
# 命中即 fail 的四组；其余两组（混淆/个人信息）仅 warn，待人工复核（ADR-0013/0029）
DIM_PERM_HARD = ("dangerous", "secrets", "injection", "exfil")
DIM_PERM_SOFT = ("obfuscation", "pii")
VERDICT_RANK = {"skipped": 0, "pass": 1, "warn": 2, "fail": 3}


def worse(a: str, b: str) -> str:
    """取两条裁决里较差的一条（多源合并用；未知值当 skipped）。"""
    return a if VERDICT_RANK.get(a, 0) >= VERDICT_RANK.get(b, 0) else b

ALL_KEYS = [f"#{i}" for i in range(1, 17)]
THREE_TIER = {"pass", "warn", "fail", "skipped"}
METHOD_STATIC = "静态检查"
METHOD_RUN = "沙箱运行"
METHOD_JUDGE = "LLM评审"
METHOD_COMPARE = "对比"

# old → new 编号（ADR-0032）；旧报告靠 report.json 顶层的 legacy_id_map 追溯。
LEGACY_ID_MAP = {"#1": "#10", "#2": "#1", "#3": "#2", "#4": "#13", "#5": "#11",
                 "#6": "#5", "#7": "#3", "#8": "#12", "#9": "#14", "#11": "#6",
                 "#12": "#15", "#13": "#4", "#15": "#16", "#16": "#7",
                 "#17": "#8", "#18": "#9"}


def load(d: Path, name: str):
    try:
        raw = (d / name).read_bytes()
    except OSError:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # 历史产物可能由旧机器的 GBK 控制台写入；兼容读取，仍失败则按缺失处理（skipped，不崩）
        try:
            text = raw.decode("gbk")
        except (UnicodeDecodeError, LookupError):
            return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def load_judge(d: Path, name: str):
    data = load(d, f"judges/{name}.json")
    sc = data.get("score") if isinstance(data, dict) else None
    if isinstance(sc, bool) or not isinstance(sc, (int, float)) or not 0 <= sc <= 1:
        return None
    return data


def _sha256_files(root: Path, files) -> str:
    """按相对路径 + 内容逐个喂进 sha256（路径也参与，改名同样改变指纹）。"""
    h = hashlib.sha256()
    for p in files:
        h.update(p.relative_to(root).as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
    return h.hexdigest()


def evaluator_fingerprint(root: Path = None) -> dict:
    """评估器指纹（ADR-0010）：scripts/*.py + judges/*.md + config.yaml + SKILL.md + reference.md 的内容 sha256。

    用途：results/meta.json 与 report.json 记录"这份报告由哪个评估器产生"；跨版本对比时
    评估器换过版 → 指标差异不再构成被测 skill 的回归证据。
    用内容指纹而非人手 semver：零维护、无法忘记更新、可复现。
    """
    root = Path(root or EVALUATOR_ROOT)
    try:
        files = sorted([p for p in list((root / "scripts").glob("*.py"))
                        + list((root / "judges").glob("*.md"))
                        + [root / "config.yaml", root / "SKILL.md", root / "reference.md"]
                        if p.is_file()],
                       key=lambda p: p.relative_to(root).as_posix())
    except OSError:
        files = []
    if not files:
        return {"version": "unknown", "content_sha256": None, "files": 0}
    digest = _sha256_files(root, files)
    return {"version": "auto-" + digest[:8], "content_sha256": digest, "files": len(files)}


def skill_fingerprint(skill_dir) -> dict:
    """被测 skill 的内容指纹（ADR-0003 的 content_sha256 兜底字段，落进 results/meta.json）。

    裸文件夹上传时结果目录名只有 auto-<hash>，vN 命名则完全看不出评的是哪份内容——
    这个字段是"单看 results 目录也能知道评的哪份 skill"的唯一来源。
    """
    if not skill_dir:
        return None
    root = Path(skill_dir)
    if not root.is_dir():
        return {"path": str(skill_dir), "content_sha256": None, "files": 0, "note": "目录不存在"}
    files = sorted((p for p in root.rglob("*")
                    if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts
                    and p.suffix != ".pyc"),
                   key=lambda p: p.relative_to(root).as_posix())
    return {"path": str(root), "content_sha256": _sha256_files(root, files), "files": len(files)}


def evalset_meta(evalset_dir) -> dict:
    """评测集来源：目录名给 name/version，同目录 meta.json 给冻结标记（reference.md § 评测集冻结）。"""
    if not evalset_dir:
        return None
    d = Path(evalset_dir)
    meta = {"name": d.parent.name, "version": d.name, "frozen": False}
    frozen = load(d, "meta.json")
    if isinstance(frozen, dict):
        meta["frozen"] = bool(frozen.get("frozen_at") or frozen.get("reviewed_by"))
        for k in ("frozen_at", "reviewed_by"):
            if frozen.get(k):
                meta[k] = frozen[k]
    else:
        meta["note"] = "无 meta.json（未冻结或旧评测集）"
    return meta


def build_meta(report: dict, skill_dir=None, evalset_dir=None) -> dict:
    """评估器写入的字段。键名避开人工字段：results/meta.json 可能已有人填的运行记录
    （model/sandbox/skipped_reason，以及字符串形式的 skill/evalset），所以用
    skill_fingerprint / evalset_source 而不是 skill / evalset（ADR-0010）。"""
    verdicts = [v["verdict"] for v in report["metrics"].values()]
    return {
        "schema": META_SCHEMA,
        "generated_at": report["generated_at"],
        "conclusion": report["conclusion"],
        "evaluator": report["evaluator"],
        "skill_fingerprint": skill_fingerprint(skill_dir),
        "metrics": {"total": len(verdicts),
                    "measured": sum(1 for v in verdicts if v != "skipped"),
                    "skipped": sum(1 for v in verdicts if v == "skipped")},
        "evalset_source": evalset_meta(evalset_dir),
    }


def write_meta(d: Path, meta: dict) -> None:
    """写 results/meta.json：已存在则**合并保留**人工填写的键，只覆盖评估器自己的键。

    人工记录（model/sandbox/skipped_reason/skill_path 等）无法由评估器重建，静默覆盖
    等于丢证据；因此评估器字段一律用自己的键名，合并时以评估器新值优先。
    """
    old = load(d, "meta.json")
    merged = {**old, **meta} if isinstance(old, dict) else meta
    _console.write_text(d / "meta.json", json.dumps(merged, ensure_ascii=False, indent=2))


def verdict_of_ratio(ratio) -> str:
    """LLM 评审分数是 pass 项数/总项数（0~1），全过 pass、有挂 warn、全挂 fail。"""
    return "pass" if ratio >= 1 else "warn" if ratio > 0 else "fail"


def verdict_of_score(score) -> str:
    return {0: "fail", 1: "warn", 2: "pass"}.get(score, "skipped")


def from_static(static, m: dict):
    if not isinstance(static, dict):
        return
    errors = static.get("errors", [])
    invoke_warn = any("disable-model-invocation" in w for w in static.get("warnings", []))
    # 旧 static.json（无 description_level）仍走旧的子串口径；新产物读结构化字段（ADR-0037）。
    m_err = any("name" in e or "description" in e for e in errors)
    token_warn = any("token" in w for w in static.get("warnings", []))
    level = static.get("description_level")
    if level == "fail":
        verdict = "fail"
    elif level == "warn":
        verdict = "warn"
    elif level == "skipped":
        verdict = "skipped"
    elif level == "ok":
        verdict = "pass"
    else:
        verdict = "fail" if m_err else "warn" if token_warn else "pass"
    chars = static.get("description_chars")
    warn = static.get("description_warn_chars")
    fail = static.get("description_fail_chars")
    if level == "skipped":
        note = "human 型：name/description 可省略（静态面 skipped，ADR-0037）"
    elif isinstance(chars, int) and isinstance(warn, int) and isinstance(fail, int):
        note = f"description {chars} 字符（警告线 {warn} / 失败线 {fail}，ADR-0037）"
    else:
        note = "name/description 规范与字符阈值"
    m["#1"] = {"verdict": verdict, "method": METHOD_STATIC,
               "data": {"description_chars": chars,
                        "description_warn_chars": warn,
                        "description_fail_chars": fail,
                        "description_tokens": static.get("description_tokens"),
                        "human": static.get("human")},
               "note": note}
    m["#3"] = {"verdict": "warn" if invoke_warn
               else "pass" if static.get("invoke", {}).get("resolved") in ("human", "both")
               else "skipped",
               "method": METHOD_STATIC, "data": static.get("invoke"),
               "note": "调用方式 frontmatter 校验（disable-model-invocation：true=human；缺省=both；"
                       "name/description 缺失不推断 human）"}
    # #4 权限最小化（ADR-0013/0029）：六组静态规则 + 可移植性闸门。
    # 四组命中即 fail；混淆/个人信息仅 warn（待人工复核）。闸门（宿主硬编码，ADR-0008）
    # 命中同样 fail——旧实现只把 errors 填非空、passed=false，而 conclusion() 只看指标裁决，
    # 导致"整体 fail"在报告里完全不可见（ADR-0030）。
    # 旧的 static.json（无新字段）走同一分支：get→None→[]，verdict 与旧版一致。
    groups = {k: (static.get(k) or []) for k in (*DIM_PERM_HARD, *DIM_PERM_SOFT)}
    gate = static.get("hardcoded") or []
    hit = [k for k in DIM_PERM_HARD if groups[k]]
    soft_hit = [k for k in DIM_PERM_SOFT if groups[k]]
    note = "六组静态规则（危险命令/凭据/注入/外发/混淆/个人信息）"
    if hit:
        note += "；命中：" + "、".join(f"{DIM_PERM_GROUPS[k]}×{len(groups[k])}" for k in hit)
    if soft_hit:
        # 硬组或闸门也命中时不能写"仅"（否则读起来像“只有警告”）
        note += ("；另有" if (hit or gate) else "；仅") + "、".join(
            f"{DIM_PERM_GROUPS[k]}×{len(groups[k])}（警告）" for k in soft_hit)
    if gate:
        note += f"；可移植性闸门命中×{len(gate)}（宿主环境硬编码，ADR-0008，命中即 fail）"
    # 排除/抑制/闸门旁证必须可见（ADR-0012/0030）：不静默丢指标
    excluded, ignored = static.get("excluded") or [], static.get("ignored") or []
    if excluded:
        shown = "、".join(excluded[:3]) + ("…" if len(excluded) > 3 else "")
        note += f"；未扫描 {len(excluded)} 处（{shown}）"
    if ignored:
        note += f"；行内抑制 {len(ignored)} 行"
    stale = static.get("stale_refs") or []
    if stale:
        note += f"；SKILL.md 声明但包内不存在的引用 {len(stale)} 处（信息行，不改裁决）"
    m["#4"] = {
        "verdict": "fail" if (hit or gate) else "warn" if soft_hit else "pass",
        "method": METHOD_STATIC,
        "data": {**groups, "hardcoded": gate, "stale_refs": stale,
                 "excluded": excluded, "ignored": ignored,
                 "scan_excluded_dirs": static.get("scan_excluded_dirs") or []},
        "note": note}


def cv_of(comp):
    """变异系数（#15，ADR-0016）：std/mean；n<2 或字段不合法 → None（不参与裁决）。

    均值 0 时与旧实现同口径（无波动 0；有波动 99，保守判不稳）。
    """
    if not isinstance(comp, dict):
        return None
    n = comp.get("n")
    if not isinstance(n, int) or isinstance(n, bool) or n < 2:
        return None
    mean = comp.get("mean")
    if isinstance(mean, bool) or not isinstance(mean, (int, float)):
        return None
    std = comp.get("std")  # score.py 已输出 std；var 是旧产物的兼容路径
    if isinstance(std, bool) or not isinstance(std, (int, float)):
        var = comp.get("var")
        if isinstance(var, bool) or not isinstance(var, (int, float)):
            return None
        std = var ** 0.5
    if mean == 0:
        return 0.0 if std == 0 else 99.0
    return std / mean


def dependency_errors(errors) -> tuple[list, list]:
    """从 trace 报错里分出「依赖缺失信号」与「agent 自己的路径笔误」（ADR-0025）。

    旧实现把任何含 not found / no such 的报错都算依赖问题——实测 agent 自己写错相对路径
    （`cat: AGENTS.md: No such file or directory`）就会被当成缺依赖（#12 假 fail）。现分两档：
    - 强信号（command not found / ModuleNotFoundError / not installed…）直接算；
    - 弱信号（no such file / not found / 不存在）还要看**缺的是什么**：包内入口（scripts/、SKILL.md、
      judges/、check-deps…）或解释器/工具（python/node/pi/git/bash…）缺失才算依赖问题；
      数据文件的 No such file 不算（agent 自己猜错产物路径 `cat: evalsets/…/meta.json` 也长这样）。
    不算的进 ignored_errors 保留可见（不静默丢）。
    """
    strong = ("command not found", "no module named", "modulenotfounderror", "importerror",
              "cannot import", "is not installed", "not installed", "not recognized as an internal")
    weak = ("no such file", "not found", "notfound", "不存在", "缺少")
    # 弱信号还要看“缺的是什么”：包内入口（脚本/SKILL.md/judges）或解释器/工具缺失才算依赖问题；
    # 数据文件的 No such file 不算——实测 agent 自己猜错产物路径（cat: evalsets/.../meta.json）会被误判。
    entry_tokens = ("skill.md", "scripts/", "check-deps", "report.py", "static_check", "judges/",
                    "python", "node", "pi ", "pi.cmd", "git", "pytest", "bash", "sh ", "npm", "pip")
    dep, ignored = [], []
    for e in errors:
        s = str(e).lower()
        if any(w in s for w in strong):
            dep.append(str(e))
        elif any(w in s for w in weak):
            (dep if any(t in s for t in entry_tokens) else ignored).append(str(e))
    return dep, ignored


def _load_path(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def build(d: Path, evalset_dir: Path | None = None) -> dict:
    m = {}
    static = load(d, "static.json")
    score = load(d, "score.json")
    idem = load(d, "idem.json")
    compare = load(d, "compare.json")
    golden = load(d, "golden.json")

    # 静态组
    for k in ("#1", "#3", "#4"):
        m[k] = {"verdict": "skipped", "method": METHOD_STATIC, "data": None, "note": "无 static.json"}
    if isinstance(static, dict):
        from_static(static, m)

    # 运行组
    m["#10"] = {"verdict": "skipped", "method": METHOD_RUN, "data": None, "note": "无触发评测数据"}
    m["#13"] = {"verdict": "skipped", "method": METHOD_RUN, "data": None, "note": "无成本数据"}
    m["#11"] = {"verdict": "skipped", "method": METHOD_RUN, "data": None, "note": "无基线数据"}
    m["#12"] = {"verdict": "skipped", "method": METHOD_RUN, "data": None, "note": "无 golden trace"}
    m["#15"] = {"verdict": "skipped", "method": METHOD_RUN, "data": None, "note": "无多次运行数据"}
    if isinstance(score, dict):
        t = score.get("trigger")
        if isinstance(t, dict):
            f1_pass = _config.get_float("report.f1_pass", 0.7)
            m["#10"] = {"verdict": "pass" if t.get("f1", 0) >= f1_pass else "fail",
                       "method": METHOD_RUN, "data": t, "note": f"F1 及格线 {f1_pass}"}
        cost = score.get("cost")
        if isinstance(cost, dict) and any(v != "skipped" for v in cost.values()):
            m["#13"] = {"verdict": "pass", "method": METHOD_RUN, "data": cost, "note": "单次运行成本统计"}
        n = score.get("necessity")
        if isinstance(n, dict):
            # #11 三分量多数票（ADR-0026）：旧口径只看 token，会把「省步不省 token」判成 fail
            # （实测 golden 步数 −22%、token +1.5%）。现三分量各投一票，多数改善才 pass。
            comps = {k: v for k, v in n.items() if isinstance(v, dict) and "improvement" in v}
            up = sum(1 for v in comps.values() if v["improvement"] > 0)
            verdict = "pass" if up * 2 > len(comps) else "warn" if up else "fail"
            samples = [v.get("n") for v in comps.values() if isinstance(v.get("n"), int)]
            sample_n = min(samples) if samples else None
            if sample_n is not None and sample_n < 3 and verdict == "pass":
                verdict = "warn"
            detail = "、".join(f"{k} {v['improvement']*100:+.1f}%" for k, v in comps.items())
            caveat = (f"；样本 n={sample_n}，少于 3 例，结论最高 warn（ADR-0033）"
                      if sample_n is not None and sample_n < 3 else "")
            m["#11"] = {"verdict": verdict, "method": METHOD_RUN, "data": n,
                       "note": f"三分量多数票（{up}/{len(comps)} 改善）：{detail}{caveat}（ADR-0026）"}
    # #10 人调用型（#3 resolved=human）不适用触发评测：pi 的 formatSkillsForPrompt 会把
    # disable-model-invocation=true 的 skill 从提示中滤除（实测：--skill 加载后模型答
    # “无 available_skills 段”）——“按 description 触发”在该载体上结构上不可能，照跑只是把
    # 载体限制记成 skill 的 fail。已实测的数据不丢，原样留在 data 里作证据，但不改判。
    if isinstance(static, dict) and (static.get("invoke") or {}).get("resolved") == "human":
        m["#10"] = {"verdict": "skipped", "method": METHOD_RUN, "data": m["#10"]["data"],
                   "note": ("调用方式为 human（disable-model-invocation=true）：不跑触发评测，"
                            "本指标不适用（SKILL.md 未声明触发式调用）")}
    # #10 触发集质量（ADR-0002 的执行检查）：照抄 description 的 prompt 会让 P/R 虚高 → 降级为 warn，
    # 不做整体闸门（与 ADR-0008 的区别：那是客观硬事实，这里是启发式阈值，疑似不该一票否决）
    ev = load(d, "evalset.json")
    if isinstance(ev, dict) and ev.get("clean") is False and m["#10"]["verdict"] != "skipped":
        m["#10"]["verdict"] = "fail" if m["#10"]["verdict"] == "fail" else "warn"
        m["#10"]["data"] = {**(m["#10"]["data"] or {}), "evalset_quality": ev}
        m["#10"]["note"] = (m["#10"].get("note") or "") + f"；触发集质量警告：{ev.get('note')}"

    # #12 双源（#29/#30）：trace 报错（实跑面）+ judges/deps.json（语义面）合成裁决。
    # 多 trace 并集（ADR-0025）：只读单条 golden.json 时，换成同 case 另一条重复运行就会翻结论
    # （实测 r1 干净、r3 带 agent 路径笔误）——所以有 traces/golden-*.json 就全部纳入。
    g_traces = [t for t in (_load_path(p) for p in sorted((d / "traces").glob("golden-*.json")))
                if isinstance(t, dict)] if (d / "traces").is_dir() else []
    g_sources = g_traces or ([golden] if isinstance(golden, dict) else [])
    if g_sources:
        all_errs = [e for t in g_sources for e in (t.get("errors") or [])]
        dep_errs, ignored_errs = dependency_errors(all_errs)
        deps = load_judge(d, "deps")
        if dep_errs:
            dep_verdict, dep_note = "fail", f"{len(g_sources)} 条 golden trace 中有依赖类报错"
        elif deps:
            dep_verdict = verdict_of_ratio(deps["score"])
            dep_note = "无依赖类报错，语义评审最小依赖（judges/deps.md，pass 项数/总项数）"
        else:
            dep_verdict, dep_note = "pass", "golden trace 中无依赖类报错即 pass"
        if ignored_errs:
            dep_note += f"；另有 {len(ignored_errs)} 条报错判为 agent 自身路径笔误（见 ignored_errors）"
        m["#12"] = {"verdict": dep_verdict, "method": METHOD_RUN,
                   "data": {"dependency_errors": dep_errs, "ignored_errors": ignored_errs,
                            "sources": len(g_sources),
                            "deps_judge": {"score": deps["score"], "items": deps.get("items")} if deps else None},
                   "note": dep_note}
    # #12 第三源：依赖全面性（ADR-0031）。缺声明是文档缺陷 → 最多降到 warn，不 fail
    # （与 trace 面"跑得通"的证据冲突时，恒不能两者同时成立）。
    miss = (static or {}).get("deps_missing") if isinstance(static, dict) else None
    if miss:
        cur = m["#12"]
        m["#12"] = {
            "verdict": worse(cur.get("verdict", "skipped"), "warn"),
            "method": METHOD_STATIC if cur.get("verdict") == "skipped" else cur.get("method"),
            "data": {**(cur.get("data") if isinstance(cur.get("data"), dict) else {}),
                     "deps_used": static.get("deps_used") or [],
                     "deps_declared": static.get("deps_declared") or [],
                     "deps_missing": miss},
            "note": ((cur.get("note") or "") + "；" if cur.get("note") else "")
                    + f"静态面：{len(miss)} 项依赖未在文档/requirements.txt/check-deps.sh 声明"
                      f"（{'、'.join(miss[:5])}）（ADR-0031）"}
    if isinstance(score, dict) and isinstance(score.get("cost"), dict) \
            and isinstance(score["cost"].get("tool_calls"), dict):
        # #15（ADR-0016）：裁决用**组内（逐 case）** 变异系数中位数——
        # pooled 会把 case 间的难度差算成“不稳定”（实测：组间均值极差 8.5 步 → pooled cv 0.65，
        # 组内中位数 0.45）。无逐 case 数据（旧产物/旧 harness）时回退 pooled 并在 note 标注口径。
        cost_data = score["cost"]
        cv_max = _config.get_float("report.cost_cv_max", 0.5)
        pooled = cost_data["tool_calls"]
        n = pooled.get("n", 1)
        pooled_cv = cv_of(pooled)
        raw_cases = score.get("cost_by_case") if isinstance(score.get("cost_by_case"), dict) else None
        cv_cases = {}
        for c, comps in (raw_cases or {}).items():
            cv = cv_of(comps.get("tool_calls") if isinstance(comps, dict) else None)
            if cv is not None:
                cv_cases[str(c)] = cv
        if n < 2:
            verdict, note = "warn", "仅单次运行，无稳定性证据"
            data = {"cv_scope": "within-case" if cv_cases else "pooled", "cost": cost_data}
        elif cv_cases:
            med = statistics.median(cv_cases.values())
            worst = max(cv_cases, key=lambda c: cv_cases[c])
            tail = (f"；pooled 口径 {pooled_cv:.2f}（含跨 case 难度差，仅作参考）"
                    if pooled_cv is not None else "")
            if med <= cv_max:
                verdict = "pass"
                note = (f"组内（逐 case）变异系数中位数 {med:.2f} ≤ {cv_max}，稳定"
                        f"（最大 {cv_cases[worst]:.2f} @ {worst}）{tail}")
            else:
                verdict = "warn"
                note = (f"组内（逐 case）变异系数中位数 {med:.2f} 超过阈值 {cv_max}，不稳定"
                        f"（最大 {cv_cases[worst]:.2f} @ {worst}）{tail}")
            data = {"cv_scope": "within-case", "cv_threshold": cv_max,
                    "cv_median": round(med, 3), "cv_max": round(cv_cases[worst], 3),
                    "cv_max_case": worst, "cv_by_case": {c: round(v, 3) for c, v in cv_cases.items()},
                    "cv_pooled": None if pooled_cv is None else round(pooled_cv, 3),
                    "cost_by_case": raw_cases, "cost": cost_data}
        else:
            stable = pooled_cv is not None and pooled_cv <= cv_max
            shown = 0.0 if pooled_cv is None else pooled_cv
            note = (f"变异系数 {shown:.2f} ≤ {cv_max}，稳定（pooled 口径：无逐 case 数据）"
                    if stable else
                    f"变异系数 {shown:.2f} > {cv_max}，不稳定（pooled 口径：无逐 case 数据）")
            verdict = "pass" if stable else "warn"
            data = {"cv_scope": "pooled", "cv_threshold": cv_max,
                    "cv_pooled": None if pooled_cv is None else round(pooled_cv, 3), "cost": cost_data}
        m["#15"] = {"verdict": verdict, "method": METHOD_RUN, "data": data, "note": note}

    # #15 跨模型一致性（ADR-0011：折进稳定性，不新增独立指标）：
    # 组内变异系数与跨模型一致率分写在同一 data 下，不混成一个数字。
    # 保守口径：跨模型只能**降级**（发现漂移），不能升级——它不能代替组内重复运行。
    mr = load(d, "model_robust.json")
    if isinstance(mr, dict) and mr.get("skipped") is False and mr.get("robust") is not None:
        rank = {"skipped": 0, "pass": 1, "warn": 2, "fail": 3}
        cross = "pass" if mr["robust"] else "warn"
        m["#15"] = {"verdict": max((m["#15"]["verdict"], cross), key=lambda v: rank.get(v, 0)),
                    "method": METHOD_RUN,
                    "data": {"intra_model_cost": m["#15"].get("data"), "model_robust": mr},
                    "note": (m["#15"].get("note") or "") + f"；跨模型一致性：{mr.get('note')}"}

    m["#14"] = {"verdict": "skipped", "method": METHOD_COMPARE, "data": None, "note": "无 expect/actual 对比"}
    if isinstance(compare, dict) and (compare.get("score") is not None
                                      or compare.get("mean_score") is not None):
        m["#14"] = {"verdict": "pass" if compare.get("match") else "fail",
                   "method": METHOD_COMPARE, "data": compare,
                   "note": "期望输出 vs 实际产出（聚合口径：多数 case 匹配即 pass）"}

    m["#16"] = {"verdict": "skipped", "method": METHOD_RUN, "data": None, "note": "无幂等数据"}
    if isinstance(idem, dict) and "idempotent" in idem:
        m["#16"] = {"verdict": "pass" if idem["idempotent"] else "fail",
                    "method": METHOD_RUN, "data": idem, "note": "重复步骤比例 ≤ 0.5 即幂等"}

    # LLM 评审组
    judges = {"#2": "brevity", "#5": "redundancy", "#6": "fallback", "#7": "precheck",
              "#8": "contract", "#9": "contract"}
    for key, name in judges.items():
        j = load_judge(d, name)
        m[key] = {"verdict": verdict_of_ratio(j["score"]) if j else "skipped",
                  "method": METHOD_JUDGE,
                  "data": ({"score": j["score"], "items": j.get("items"),
                            "evidence": j["evidence"], "reason": j["reason"]} if j else None),
                  "note": (name + "（逐项计分：" + "/".join(
                      f"{i['name']}={'✓' if i['pass'] else '✗'}" for i in j.get("items", [])) + "）")
                  if j else "无评审输出"}

    # #4 语义面（ADR-0029）：工号/客户名/内部系统地址这类公司特化信息没有可枚举格式，
    # 只能靠评审。与静态面（六组 + 闸门）取**较差者**（ADR-0017 多源口径：主源 + 辅证）。
    pii_j = load_judge(d, "pii")
    if pii_j:
        cur = m["#4"]
        items = "/".join(f"{i['name']}={'✓' if i['pass'] else '✗'}" for i in pii_j.get("items", []))
        m["#4"] = {
            "verdict": worse(cur.get("verdict", "skipped"), verdict_of_ratio(pii_j["score"])),
            "method": METHOD_JUDGE if cur.get("verdict") == "skipped" else cur.get("method"),
            "data": {**(cur.get("data") if isinstance(cur.get("data"), dict) else {}),
                     "pii_judge": {"score": pii_j["score"], "items": pii_j.get("items"),
                                   "evidence": pii_j.get("evidence"), "reason": pii_j.get("reason")}},
            "note": ((cur.get("note") or "") + "；" if cur.get("note") else "")
                    + f"语义面 pii 评审（逐项：{items}）：{pii_j.get('reason') or ''}"}

    # #2 双源（ADR-0017）：正文行数 = 可数事实 → 静态面主源（超线即 warn/fail，机械证据优先）；
    # 语义两项 → LLM 面辅证，结果原形状保留在同一份 data 里，不丢。
    if isinstance(static, dict):
        lines = static.get("skill_md_body_lines")
        if isinstance(lines, int):
            level = static.get("body_level")
            warn = static.get("skill_md_body_warn_lines")
            fail = static.get("skill_md_body_fail_lines")
            limit = static.get("skill_md_body_max_lines")  # 旧字段 = warn 线，兼容旧 static.json
            if level not in ("ok", "warn", "fail"):
                level = "warn" if isinstance(limit, int) and lines > limit else "ok"
            cur = m["#2"]
            merged = {"skill_md_body_lines": lines, "body_level": level}
            for key, value in (("skill_md_body_warn_lines", warn),
                               ("skill_md_body_fail_lines", fail),
                               ("skill_md_body_max_lines", limit)):
                if isinstance(value, int):
                    merged[key] = value
            if isinstance(cur.get("data"), dict):
                merged.update(cur["data"])
            if level == "fail":
                m["#2"] = {"verdict": "fail", "method": METHOD_STATIC, "data": merged,
                           "note": f"正文 {lines} 行 > 失败阈值 {fail} 行（静态面主源，ADR-0037）；"
                                   + (cur.get("note") or "无语义评审输出")}
            elif level == "warn":
                m["#2"] = {"verdict": "warn", "method": METHOD_STATIC, "data": merged,
                           "note": f"正文 {lines} 行 > 警告阈值 {warn} 行（静态面主源，ADR-0037）；"
                                   + (cur.get("note") or "无语义评审输出")}
            elif isinstance(cur.get("data"), dict):
                m["#2"] = {**cur, "data": merged,
                           "note": (cur.get("note") or "")
                                   + f"；正文 {lines} 行 ≤ 警告阈值（静态面）"}
            else:
                m["#2"] = {"verdict": "skipped", "method": METHOD_STATIC, "data": merged,
                           "note": f"正文 {lines} 行 ≤ 警告阈值（静态面通过）；无语义评审输出"}



    # 评测集 AI 审核（#49）：reviewed_by=ai 时给依赖评测集的指标加标注
    reviewed_by = _evalset_reviewed_by(d, evalset_dir)
    if reviewed_by == "ai":
        ai_note = "评测集 AI 审核，未经人工复核"
        for k in ("#10", "#11", "#14"):
            if m[k]["verdict"] != "skipped":
                m[k]["note"] = f"{m[k]['note']}（{ai_note}）"

    from datetime import datetime
    report = {"schema": 2, "legacy_id_map": LEGACY_ID_MAP,
              "metrics": m, "conclusion": conclusion(m),
              "advice": _advice(m),
              "reviewed_by": reviewed_by,
              "evaluator": evaluator_fingerprint(),
              "generated_at": datetime.now().isoformat(timespec="seconds")}
    return report


def _evalset_reviewed_by(d: Path, evalset_dir: Path | None = None):
    """读评测集 meta.json 的 reviewed_by（human|ai|None）。结果目录 =
    evalsets/<name>/results/<version>，而评测集版本是 evalsets/<name>/v<N>/——
    版本目录名与结果版本号无关（如 auto-b9d7fab vs v1），不能拼接，只能 glob。
    --evalset 显式指定优先；未指定时取 v* 中版本号最大的冻结 meta。"""
    mdir = evalset_dir
    if mdir is None:
        base = d.parent.parent  # evalsets/<name>
        vers = []
        for p in base.glob("v*/meta.json"):
            m = re.fullmatch(r"v(\d+)", p.parent.name)
            if m:
                vers.append((int(m.group(1)), p.parent))
        mdir = max(vers)[1] if vers else None
    meta = load(mdir, "meta.json") if mdir else None
    if isinstance(meta, dict) and meta.get("reviewed_by") in ("human", "ai"):
        return meta["reviewed_by"]
    return None


def conclusion(m: dict) -> str:
    verdicts = [v["verdict"] for v in m.values()]
    if "fail" in verdicts:
        return "has-failures"
    if "warn" in verdicts or "skipped" in verdicts:
        return "with-warnings"
    return "all-pass"


FLOW_STEPS = [  # SKILL.md 的 8 步流程（#43 流程图数据源）
    ("1 前置自检", "scripts/judges/reference.md 存在；被测路径有 SKILL.md，缺失走 Fallback"),
    ("2 存档与定版", "只读存档 uploads/，副本 git init+commit，版本号 = 短 hash"),
    ("3 静态检查", "static_check.py：#1 规范、#3 调用方式、#4 六组规则、可移植性闸门"),
    ("4 评测集", "检查冻结的 evalsets/<name>/vN/；未冻结则生成 → 条数闸门 + 触发集质量自检 → LLM 自动审核（换模型）即冻结（reviewed_by=ai）"),
    ("5 沙箱运行", "按档增量跑 pi CLI：T2A 触发（early-exit）→ T2B 真实场景 A/B → T3 主运行+重复（可选跨模型 model_robust.py）"),
    ("6 LLM 评审", "judge_run.py（默认 llm 直连）逐项计分，judge_runner.py --validate 通过才进报告"),
    ("7 计算", "score.py（P/R/F1、成本、三分量必要性）+ idem.py（幂等）"),
    ("8 报告", "report.py：report.json + report.md（--html 加静态页），16 指标恒出现"),
]


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _flow_svg() -> str:
    # 内嵌 SVG：竖排 8 盒 + 箭头，每盒内标注该步测试方法
    rows = []
    y = 10
    for name, desc in FLOW_STEPS:
        rows.append(
            f'<rect x="20" y="{y}" width="700" height="46" rx="6" class="stepbox"/>'
            f'<text x="34" y="{y + 19}" class="stepname">{_esc(name)}</text>'
            f'<text x="34" y="{y + 38}" class="stepdesc">{_esc(desc[:60])}</text>'
            f'<line x1="370" y1="{y + 46}" x2="370" y2="{y + 58}" class="arrow"/>'
            f'<polygon points="366,{y + 56} 374,{y + 56} 370,{y + 64}" class="arrowhead"/>')
        y += 64
    return (f'<svg viewBox="0 0 740 {y}" role="img" aria-label="评估流程图" '
            f'xmlns="http://www.w3.org/2000/svg">{"".join(rows)}</svg>')


def _skill_meta(skill_arg=None, evalset_dir=None):
    """被测 skill 的 name/description（SKILL.md frontmatter），供审核视图顶部展示。
    --skill 指定优先；缺省兜底读评估器 uploads/<name>-<时间戳>/SKILL.md 只读存档（取最新）。"""
    p = None
    if skill_arg is not None:
        p = skill_arg if skill_arg.is_file() else skill_arg / "SKILL.md"
    elif evalset_dir is not None:
        up = evalset_dir.parent.parent.parent / "uploads"
        cands = sorted(up.glob(f"{evalset_dir.parent.name}-*/SKILL.md"))
        p = cands[-1] if cands else None
    if p is None or not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.match(r"\s*---\s*\n(.*?)\n\s*---", text, re.S)
    fm = m.group(1) if m else ""

    def field(k):
        mm = re.search(rf"^{k}:\s*(.+)$", fm, re.M)
        return mm.group(1).strip().strip("\"'") if mm else ""

    name, desc = field("name"), field("description")
    if not name and not desc:
        return None
    return {"name": name, "description": desc, "source": str(p)}


def _evalset_html(evalset_dir, skill_meta=None) -> str:
    if not evalset_dir or not evalset_dir.is_dir():
        return "<p class='hint'>未指定评测集目录（--evalset），审核视图不可用。</p>"
    rows = []
    idx = 0
    for group, sub in (("应触发", "triggers/should"), ("不应触发", "triggers/should-not"),
                       ("易混淆", "triggers/confusable"), ("结果用例", "cases")):
        d = evalset_dir / sub
        if not d.is_dir():
            continue
        items = []
        for f in sorted(d.glob("*.json")):
            try:
                j = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            expect = j.get("expect")
            idx += 1
            items.append(
                f"<div class='card rv-item' data-group='{group}' data-file='{f.name}' data-idx='{idx}' "
                f"data-prompt='{_esc(j.get('prompt', ''))}' data-expect='{_esc(expect or '')}'>"
                f"<div><code>{_esc(j.get('prompt', ''))}</code>"
                + (f" → 期望：{_esc(expect)}" if expect else "") +
                f" <small style='color:#94a3b8'>{f.name}</small></div>"
                f"<div class='rv-ctrl'>"
                f"<button type='button' class='rv-ok' onclick='rv(this,&quot;approve&quot;)'>✓ 通过</button> "
                f"<button type='button' class='rv-bad' onclick='rv(this,&quot;reject&quot;)'>✗ 驳回</button> "
                f"<input class='rv-note' placeholder='驳回原因（驳回必填）'"
                f" oninput='this.closest(&quot;.rv-item&quot;).dataset.note=this.value'>"
                f"</div></div>")
        if items:
            rows.append(f"<h4>{group}（{len(items)}）</h4>" + "".join(items))
    if not rows:
        return "<p class='hint'>评测集目录为空。</p>"
    name = _esc(evalset_dir.parent.name)
    ver = _esc(evalset_dir.name)
    total = idx
    head = ""
    if skill_meta:
        head = ("<div class='card' id='rv-skill'><strong>被测 Skill："
                f"{_esc(skill_meta['name'])}</strong>"
                f"<p style='margin:4px 0 0;font-size:13px;color:#475569'>"
                f"{_esc(skill_meta['description'])}</p></div>")
    else:
        head = ("<p class='hint'>未找到被测 skill 的 SKILL.md（可用 --skill 指定路径），"
                "审核视图不展示 name/description。</p>")
    return (
        head
        + f"<div id='rv-bar'><strong>审核进度：</strong><span id='rv-count'>0/{total}</span> "
        f"<button type='button' id='rv-submit' onclick='rvSubmit()'>提交 review.json</button> "
        f"<button type='button' onclick='rvExport(true)'>复制</button> "
        f"<button type='button' onclick='rvExport(false)'>下载</button>"
        f"<span id='rv-submit-status' style='font-size:13px'></span></div>"
        "<div class='hint' style='margin:8px 0'>审核标准："
        "应触发——像真实用户随口说的话、确实该进登记流程；"
        "不应触发——确实不该触发（查询/实现/其它系统）；"
        "易混淆——边界成立、模型容易踩且不该触发的；"
        "结果用例——期望输出必须可判定（能对着回答判对错）。"
        "逐条标 通过/驳回，驳回必须写原因；全部审完后点「提交 review.json」，"
        "选到 evalsets/&lt;name&gt;/reviews/ 目录保存（浏览器会记住上次目录，之后一键直达）；"
        "保存后告诉评估 agent 审核已完成。复制/下载按钮保留作兜底。</div>"
        f"<div data-name='{name}' data-ver='{ver}' id='rv-root'>{'' .join(rows)}</div>"
        f"<textarea id='rv-out' style='display:none'></textarea>"
    )


_HTML_TMPL = '''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Skill 评估报告</title><style>
body{font-family:system-ui,sans-serif;margin:0;background:#f6f7f9;color:#1c2733}
header{padding:16px 24px;background:#1f2937;color:#fff}
.badge{padding:2px 10px;border-radius:10px;font-size:13px}
.pass{background:#0b7a3d}.warn{background:#b7791f}.fail{background:#b23b3b}.skipped{background:#6b7280}
.badge.vf-pass{background:#0b7a3d}.badge.vf-warn{background:#b7791f}.badge.vf-fail{background:#b23b3b}.badge.vf-skipped{background:#6b7280}
.vf-pass{color:#0b7a3d}.vf-warn{color:#b7791f}.vf-fail{color:#b23b3b}.vf-skipped{color:#6b7280}
/* 徽章 = 同色底 + 白字：`.vf.badge` 永不命中（_badge() 只发 badge+vf-x，没有裸 .vf），
   会让 .vf-x 的彩色字压在 .badge.vf-x 的同色底上 → 完全看不见。按四档显式配对。 */
.badge.vf-pass,.badge.vf-warn,.badge.vf-fail,.badge.vf-skipped{color:#fff}
.adv{background:#fff8e6;border-left:3px solid #b7791f;border-radius:4px;padding:6px 10px;margin-top:6px;font-size:13px}
.adv-item{font-size:13px}#overview td:first-child{font-weight:600;white-space:nowrap}
nav{display:flex;gap:4px;padding:8px 24px;background:#fff;border-bottom:1px solid #ddd}
nav button{border:0;background:none;padding:8px 16px;cursor:pointer;font-size:14px}
nav button.on{border-bottom:2px solid #1f2937;font-weight:600}
main{padding:16px 24px}.tab{display:none}.tab.on{display:block}
card,.card{background:#fff;border:1px solid #e2e6ea;border-radius:8px;padding:12px 16px;margin:10px 0}
code,pre{background:#eef1f4;border-radius:4px;font-size:13px}
pre{overflow:auto;padding:8px}.stepbox{fill:#fff;stroke:#94a3b8}
.stepname{font-weight:700;font-size:14px}.stepdesc{font-size:12px;fill:#475569}
.arrow{stroke:#64748b;stroke-width:1.5}.arrowhead{fill:#64748b}
h4{margin:14px 0 4px}.hint{color:#6b7280}
#rv-bar{position:sticky;top:0;background:#fff;border:1px solid #e2e6ea;border-radius:8px;padding:8px 12px;z-index:2;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
#rv-bar button{border:1px solid #1f2937;background:#1f2937;color:#fff;border-radius:6px;padding:4px 12px;cursor:pointer;font-size:13px}
.rv-item .rv-ctrl{margin-top:6px;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.rv-ctrl button{border:1px solid #94a3b8;background:#fff;border-radius:6px;padding:2px 10px;cursor:pointer;font-size:13px}
.rv-item[data-verdict="approve"]{border-color:#0b7a3d}.rv-item[data-verdict="approve"] .rv-ok{background:#0b7a3d;color:#fff}
.rv-item[data-verdict="reject"]{border-color:#b23b3b}.rv-item[data-verdict="reject"] .rv-bad{background:#b23b3b;color:#fff}
.rv-note{flex:1;min-width:180px;border:1px solid #dde2e7;border-radius:6px;padding:3px 8px;font-size:13px}
table{border-collapse:collapse;width:100%;font-size:13px}
td,th{border:1px solid #dde2e7;padding:4px 8px;text-align:left}
details summary{cursor:pointer;color:#475569;font-size:13px}
details pre{max-height:420px}
</style></head><body>
<header><h1 style="margin:0;font-size:20px">Skill 评估报告</h1>
<p style="margin:4px 0 0">结论：<span class="badge __CLS__">__CONCLUSION__</span>__META__</p></header>
<nav><button data-t="report" class="on" onclick="tab('report')">报告</button>
<button data-t="advice" onclick="tab('advice')">优化建议</button>
<button data-t="evalset" onclick="tab('evalset')">评测集审核</button>
<button data-t="flow" onclick="tab('flow')">评估流程</button></nav>
<main>
<section class="tab on" id="t-report">__CARDS__</section>
<section class="tab" id="t-advice">__ADVICE__</section>
<section class="tab" id="t-evalset">__EVALSET__</section>
<section class="tab" id="t-flow">__FLOW__</section>
</main>
<script>function tab(n){document.querySelectorAll('.tab').forEach(
 e=>e.classList.toggle('on',e.id==='t-'+n));
 document.querySelectorAll('nav button').forEach(
 e=>e.classList.toggle('on',e.dataset.t===n));}
function rv(btn,v){var it=btn.closest('.rv-item');
 it.dataset.verdict=v;rvCount();}
function rvCount(){var all=document.querySelectorAll('.rv-item'),done=0;
 all.forEach(function(e){if(e.dataset.verdict)done++;});
 var c=document.getElementById('rv-count');if(c)c.textContent=done+'/'+all.length;}
function rvBuild(){var root=document.getElementById('rv-root');if(!root)return null;
 var bad=[];
 document.querySelectorAll('.rv-item').forEach(function(e){
  if(e.dataset.verdict==='reject'&&!((e.dataset.note||'').trim()))bad.push(e.dataset.file);
  if(!e.dataset.verdict)bad.push(e.dataset.file+' 未标');});
 if(bad.length){alert('以下用例未完成审核或驳回缺原因：'+bad.join('；'));return null;}
 var items=[];
 document.querySelectorAll('.rv-item').forEach(function(e){
  items.push({group:e.dataset.group,file:e.dataset.file,verdict:e.dataset.verdict,
   note:e.dataset.note||'',prompt:e.dataset.prompt,expect:e.dataset.expect});});
 var out={type:'evalset-review',evalset:root.dataset.name,version:root.dataset.ver,
  reviewed_at:new Date().toISOString(),items:items};
 return JSON.stringify(out,null,2);}
function rvExport(copy){var text=rvBuild();if(text===null)return;
 var ta=document.getElementById('rv-out');ta.value=text;ta.style.display='block';
 if(copy){navigator.clipboard.writeText(text).then(
  function(){alert('已复制到剪贴板，粘贴发给评估 agent 即可');},
  function(){ta.select();document.execCommand('copy');});}
 else{rvDownload(text);}}
function rvDownload(text){var root=document.getElementById('rv-root');
 var a=document.createElement('a');
 a.href=URL.createObjectURL(new Blob([text],{type:'application/json'}));
 a.download=(root?root.dataset.name:'evalset')+'-'+(root?root.dataset.ver:'v1')+'-review.json';a.click();}
function rvDone(msg){var el=document.getElementById('rv-submit-status');
 if(el){el.style.color='#0b7a3d';el.style.fontWeight='600';el.textContent='✓ 已提交 '+msg;}}
function rvSubmit(){var root=document.getElementById('rv-root');if(!root)return;
 var text=rvBuild();if(text===null)return;
 var fname=root.dataset.name+'-'+root.dataset.ver+'-review.json';
 if(window.showSaveFilePicker){
  window.showSaveFilePicker({suggestedName:fname,
   types:[{description:'JSON',accept:{'application/json':['.json']}}]})
   .then(function(h){return h.createWritable().then(function(w){
    return w.write(text).then(function(){return w.close();});}).then(function(){rvDone(h.name);});})
   .catch(function(err){
    if(err&&err.name==='AbortError')return; /* 用户取消选择：静默，不算失败 */
    var el=document.getElementById('rv-submit-status');
    if(el){el.style.color='#b23b3b';el.style.fontWeight='600';
     el.textContent='✗ 保存失败：'+((err&&err.message)||err||'未知错误')+'（可用复制/下载兑底）';}});}
 else{rvDownload(text);
  rvDone(fname+'（浏览器不支持直接落盘，已下载，请移到 evalsets/'+root.dataset.name+'/reviews/）');}}</script>
</body></html>'''



# data 渲染口径（见 reference.md § 报告契约）：表格优先 → 原始 JSON 折叠；短 data 内联；**不静默截断**
# （旧实现 [:2000] 会把 #16 这种 2165 字符的 data 切成非法 JSON）


def _fmt(v, nd=3) -> str:
    """数值 → 短字符串（浮点去尾零）；None → —。"""
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, float):
        s = f"{v:.{nd}f}".rstrip("0").rstrip(".")
        return s or "0"
    return str(v)


def _cell(v) -> str:
    """表格单元格文本：压平空白；超 ui.cell_max 截断（全文仍在折叠的原始 data 里）。"""
    if v is None:
        return ""
    text = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
    text = " ".join(text.split())
    limit = _config.get_int("ui.cell_max", 800)
    return text if len(text) <= limit else text[:limit] + "…（截断，全文见原始 data）"


def _md_cell(v) -> str:
    return _cell(v).replace("|", "\\|")


def _data_table(metric: str, data):
    """已知形状的 data → (摘要行, 列名, 行)；无表可渲染返回 None。

    只覆盖“同构行数组”型 data（#16/#15/#13/#14），形状与 report.json 同源，
    渲染层不得反过来改 report.json 的形状。"""
    if not isinstance(data, dict):
        return None
    if metric == "#16":
        pairs = data.get("per_pair")
        if not isinstance(pairs, list) or not pairs:
            return None
        summary = (f"ratio {_fmt(data.get('ratio'))} · 幂等 {_fmt(data.get('idempotent_pairs'))}"
                   f"/{_fmt(data.get('pairs'))} 对 · 判定："
                   f"{'幂等' if data.get('idempotent') else '不幂等'}")
        rows = [[p.get("case"), p.get("pair"), _fmt(p.get("ratio")),
                 f"{_fmt(p.get('repeated_steps'))}/{_fmt(p.get('total_steps'))}",
                 "✓" if p.get("idempotent") else "✗"]
                for p in pairs if isinstance(p, dict)]
        return summary, ["case", "pair", "ratio", "repeated/total", "幂等"], rows
    if metric == "#15":
        per = data.get("cost_by_case")
        if not isinstance(per, dict) or not per:
            return None
        cv_by_case = data.get("cv_by_case") if isinstance(data.get("cv_by_case"), dict) else {}
        summary = (f"口径 {data.get('cv_scope')} · cv 中位数 {_fmt(data.get('cv_median'))}"
                   f" · 最大 {_fmt(data.get('cv_max'))}@{_fmt(data.get('cv_max_case'))}"
                   f" · pooled {_fmt(data.get('cv_pooled'))} · 阈值 {_fmt(data.get('cv_threshold'))}")
        rows = []
        for c, comps in per.items():
            comps = comps if isinstance(comps, dict) else {}
            tc = comps.get("tool_calls") if isinstance(comps.get("tool_calls"), dict) else {}
            rows.append([c,
                         f"{_fmt(tc.get('mean'))} ({_fmt(tc.get('std'))}, {_fmt(tc.get('n'))})",
                         _fmt((comps.get("tokens") or {}).get("mean")),
                         _fmt((comps.get("seconds") or {}).get("mean")),
                         _fmt(cv_by_case.get(c))])
        return summary, ["case", "tool_calls 均值(标准差, n)", "tokens 均值", "秒 均值", "组内 cv"], rows
    if metric == "#13":
        labels = {"tool_calls": "工具步数", "tokens": "tokens", "seconds": "秒"}
        rows = [[labels[k]] + [_fmt((data.get(k) or {}).get(x)) for x in ("mean", "std", "n", "min", "max")]
                for k in ("tool_calls", "tokens", "seconds") if isinstance(data.get(k), dict)]
        if not rows:
            return None
        return f"单次运行成本（{len(rows)} 个分量，pooled）", ["分量", "均值", "标准差", "n", "最小", "最大"], rows
    if metric == "#14":
        per = data.get("per_case")
        if not isinstance(per, list) or not per:
            return None
        summary = f"mean_score {_fmt(data.get('mean_score'))} · {data.get('note') or ''}".strip(" ·")
        rows = [[p.get("name"), "✓" if p.get("match") else "✗", _fmt(p.get("score")), _cell(p.get("reason"))]
                for p in per if isinstance(p, dict)]
        return summary, ["case", "匹配", "score", "理由"], rows
    return None


def _html_table(summary, cols, rows) -> str:
    head = "".join(f"<th>{_esc(c)}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in r) + "</tr>" for r in rows)
    out = (f"<p style='margin:6px 0 2px;font-size:13px;color:#475569'>{_esc(summary)}</p>"
           if summary else "")
    return out + f"<table><tr>{head}</tr>{body}</table>"


def _md_table(summary, cols, rows) -> list:
    out = ([summary, ""] if summary else [])
    out.append("| " + " | ".join(str(c) for c in cols) + " |")
    out.append("|" + "---|" * len(cols))
    out += ["| " + " | ".join(_md_cell(c) for c in r) + " |" for r in rows]
    return out


def _raw_html(data, folded=False) -> str:
    """原始 JSON：有表就折叠（不管多短），无表且短才内联——不再静默截断（旧实现 [:2000] 会切断 JSON）。

    折叠不是省略：表格是视图，折叠里是原文（ADR-0030，HTML 不得比 md/json 少信息）。
    """
    raw = json.dumps(data, ensure_ascii=False, indent=2)
    if folded or len(raw) > _config.get_int("ui.inline_max", 400):
        return (f"<details><summary>原始 JSON（{len(raw)} 字符，与 report.json 同源）</summary>"
                f"<pre>{_esc(raw)}</pre></details>")
    return f"<pre>{_esc(raw)}</pre>"


VF_GLYPH = {"pass": "✓", "fail": "✗", "warn": "⚠", "skipped": "–"}  # 结果栏符号（ADR-0030）
ADV_SEV_CLS = {"fail": "fail", "warn": "warn", "info": "skipped"}
ADV_SEV_RANK = {"fail": 0, "warn": 1, "info": 2}

# 每条指标的建议动作（确定性文本，问题与建议同源 → 结构上无法服钩，ADR-0030）
ADVICE_ACTIONS = {
    "#1": "把 description 压到字符阈值内（>384 警告，>1024 失败），并确认 name/description 齐全可读",
    "#2": "把细节下沉到 reference.md，SKILL.md 只留立即要做的步骤",
    "#3": "确认调用方式：human 型加 disable-model-invocation: true，模型触发型留空",
    "#4": "按命中项处置：凭据轮换、个人信息脱敏、危险命令改可逆操作、宿主硬编码改相对路径",
    "#5": "删掉与附属文件重复、复读 frontmatter 或客套的段落",
    "#6": "给需要凭据/网络/外部服务的步骤补降级路径",
    "#7": "补齐前置自检脚本，并在 SKILL.md 里声明自检不通过就不继续",
    "#8": "明确输入契约：必填/可选、缺失与非法时的处理",
    "#9": "明确输出契约：路径、格式，以及失败时留下什么",
    "#10": "修 description 或触发集：应触发项未触发看召回率，不应触发被触发看精确率",
    "#11": "删掉不改变产出的步骤（与无 skill 基线对比；省步不省 token 也算必要性不足）",
    "#12": "补齐依赖声明（SKILL.md 依赖小节 + requirements.txt/check-deps.sh），并复跑 golden trace",
    "#13": "合并重复调用、减少不必要的读取，把单次运行成本降到最低",
    "#14": "把期望输出写成可判定的产物（文件/字段），不要只写自然语言描述",
    "#15": "定位波动最大的 case：先对齐多次运行的输入，再消除随机步骤",
    "#16": "让重复步骤幂等：重跑前清理产物，或加已完成短路判断",
}


def _badge(verdict: str) -> str:
    v = verdict if verdict in VF_GLYPH else "skipped"
    return f"<span class='badge vf-{v}'>{VF_GLYPH[v]} {_esc(v)}</span>"


def _glyph(verdict: str) -> str:
    v = verdict if verdict in VF_GLYPH else "skipped"
    return f"<span class='vf vf-{v}'>{VF_GLYPH[v]}</span>"


def _advice_items(data: dict) -> list:
    """取该指标下所有 judge 检查点组。

    检查点不一定在顶层：顶层 items 是普通评审（#2/#5/#6/#7–9），
    而 #12 的 deps 评审嵌在 `deps_judge`、#4 的 pii 评审嵌在 `pii_judge`——
    只看顶层会永远取不到这两类（docstring 声称的"检查点级"就成了空头支票）。
    """
    out = []
    if isinstance(data.get("items"), list):
        out.append(data["items"])
    for sub in ("deps_judge", "pii_judge"):
        node = data.get(sub)
        if isinstance(node, dict) and isinstance(node.get("items"), list):
            out.append(node["items"])
    return out


def _judge_rows(items) -> list:
    """judge items → 表格行（检查点 / 结果 / 原文引用）。

    只给 md 与 _tables 用：HTML 侧另有一套带颜色符号的渲染（`_glyph`，
    颜色不能过 `_html_table` 的转义），两者输出格式不同，不强行合并。
    """
    return [[i.get("name"), "✓" if i.get("pass") else "✗", _cell(i.get("quote"))]
            for i in items if isinstance(i, dict)]


def _advice(m: dict) -> list:
    """从裁决确定性派生优化建议（ADR-0030）：每条带 #N 反链，问题与建议同源。

    三档粒度：指标级（非 pass 即一条）、检查点级（judge 里 pass=false 的项）、
    #4 命中级（每个命中的规则组/闸门一条）。排序 fail → warn → info，同级按指标号。
    """
    out = []
    for k in ALL_KEYS:
        v = m.get(k) or {}
        verdict = v.get("verdict")
        if verdict == "pass":
            continue
        sev = {"fail": "fail", "warn": "warn"}.get(verdict, "info")
        data = v.get("data") if isinstance(v.get("data"), dict) else {}
        phrase = {"fail": "未通过", "warn": "有警告"}.get(verdict, "未测")
        # 未测不是问题：给“补测”动作，不给“修问题”的方子——
        # 否则 info 档上问题与建议仍然脱钩（评审 P1）。
        action = (f"补测：按「{v.get('method') or '未指定'}」面拿到数据后重出报告（未测是缺口，不是问题）"
                  if verdict == "skipped" else ADVICE_ACTIONS.get(k, "复跑该指标的测量面"))
        out.append({"metric": k, "severity": sev,
                    "problem": f"{DIM_NAMES.get(k, k)}（{k}）{phrase}",
                    "evidence": (v.get("note") or "").strip() or "—", "action": action})
        for items in _advice_items(data):
            for it in items:
                if isinstance(it, dict) and it.get("pass") is False:
                    out.append({"metric": k, "severity": sev,
                                "problem": f"检查点未通过：{it.get('name')}",
                                "evidence": str(it.get("quote") or "").strip() or "—",
                                "action": action})
        if k != "#4":
            continue
        for g, label in DIM_PERM_GROUPS.items():
            hits = [h for h in (data.get(g) or []) if isinstance(h, dict)]
            if hits:
                out.append({"metric": k,
                            "severity": "fail" if g in DIM_PERM_HARD else sev,
                            "problem": f"静态规则命中：{label}（{g}）×{len(hits)}",
                            "evidence": "；".join(f"{h.get('file')}:{h.get('line')}" for h in hits[:3]),
                            "action": ADVICE_ACTIONS["#4"]})
        gate = [h for h in (data.get("hardcoded") or []) if isinstance(h, dict)]
        if gate:
            out.append({"metric": k, "severity": "fail",
                        "problem": f"可移植性闸门命中：宿主环境硬编码 ×{len(gate)}",
                        "evidence": "；".join(f"{h.get('file')}:{h.get('line')} {h.get('text')}"
                                               for h in gate[:3]),
                        "action": "把绝对路径/宿主目录改成相对路径或环境变量（ADR-0008）"})
    out.sort(key=lambda a: (ADV_SEV_RANK.get(a["severity"], 3),
                             int(a["metric"][1:]) if a["metric"][1:].isdigit() else 999))
    return out


def _flatten_table(data):
    """通用兜底：任何 dict/list 递归压平成 key-value 表——渲染层不留裸 JSON（ADR-0030）。"""
    rows = []

    def walk(v, path):
        if isinstance(v, dict):
            for key in v:
                walk(v[key], f"{path}.{key}" if path else str(key))
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            for i, x in enumerate(v):
                walk(x, f"{path}[{i}]")
        else:
            rows.append([path or "值", _cell(v)])

    walk(data, "")
    return [(None, ["字段", "值"], rows)] if rows else []


def _tables(metric: str, data, allow_generic=True) -> list:
    """指标 data → 表格列表（0..n）。先试旧形状，再试本指标定制表，最后通用兜底。

    只读不改：形状与 report.json 同源，渲染层不得反过来改 report.json 的形状。
    allow_generic=False：调用方已用 items 渲染出表时，不再把同一份 dict 再压一遍。
    """
    out = []
    known = _data_table(metric, data)
    if known:
        out.append(known)
    if isinstance(data, dict):
        if metric == "#10" and "f1" in data:
            out.append(("触发精准度（应触发/不应触发/易混淆三组）", ["分量", "值"],
                        [["精确率", _fmt(data.get("precision"))],
                         ["召回率", _fmt(data.get("recall"))], ["F1", _fmt(data.get("f1"))]]))
        elif metric == "#11":
            labels = {"tokens": "tokens", "tool_calls": "工具步数", "seconds": "秒"}
            rows = [[labels.get(key, key), _fmt(comp.get("golden")), _fmt(comp.get("baseline")),
                     f"{comp['improvement'] * 100:+.1f}%"]
                    for key, comp in data.items()
                    if isinstance(comp, dict) and "improvement" in comp]
            if rows:
                out.append(("有 skill（golden）对比无 skill（baseline）",
                            ["分量", "有 skill 均值", "无 skill 基线", "提升率"], rows))
        elif metric == "#12":
            rows = [["依赖类报错", _cell(e)] for e in data.get("dependency_errors") or []]
            rows += [["忽略的报错（agent 自身路径笔误）", _cell(e)]
                     for e in data.get("ignored_errors") or []]
            if rows:
                out.append((f"golden trace 来源 {_fmt(data.get('sources'))} 条", ["类型", "内容"], rows))
            dj = data.get("deps_judge")
            if isinstance(dj, dict) and isinstance(dj.get("items"), list):
                out.append((f"语义面 deps 评审 score {_fmt(dj.get('score'))}",
                            ["检查点", "结果", "原文引用"], _judge_rows(dj["items"])))
            miss = data.get("deps_missing") or []
            if miss:
                out.append((f"依赖全面性（ADR-0031）：已用 {len(data.get('deps_used') or [])} 项、"
                            f"已声明 {len(data.get('deps_declared') or [])} 项",
                            ["未声明的依赖", "说明"],
                            [[m, "未在文档/requirements.txt/check-deps.sh 声明"] for m in miss]))
        elif metric == "#4":
            level = {**{g: "error → fail" for g in DIM_PERM_HARD},
                     **{g: "warning（人工复核）" for g in DIM_PERM_SOFT}}
            rows = [[f"{DIM_PERM_GROUPS.get(g, g)}（{g}）", len(data.get(g) or []), level.get(g, "")]
                    for g in (*DIM_PERM_HARD, *DIM_PERM_SOFT) if g in data]
            rows.append(["可移植性闸门（hardcoded）", len(data.get("hardcoded") or []),
                         "error → fail（ADR-0008）"])
            out.append(("六组静态规则 + 可移植性闸门（ADR-0013/0029）",
                        ["规则组", "命中数", "级别"], rows))
            hits = [(g, h) for g in (*DIM_PERM_HARD, *DIM_PERM_SOFT, "hardcoded")
                    for h in (data.get(g) or []) if isinstance(h, dict)]
            if hits:
                out.append((None, ["规则组", "文件", "行", "命中文本"],
                            [[g, h.get("file"), h.get("line"), _cell(h.get("text"))] for g, h in hits]))
            out.append(("扫描范围与旁证（信息行，不改裁决，ADR-0012/0030）", ["项目", "值"],
                        [["声明但包内不存在的引用", "、".join(data.get("stale_refs") or []) or "无"],
                         ["未扫描", "、".join(data.get("excluded") or []) or "无"],
                         ["行内抑制", f"{len(data.get('ignored') or [])} 行"],
                         ["默认豁免目录存在者",
                          "、".join(data.get("scan_excluded_dirs") or []) or "无"]]))
            pj = data.get("pii_judge")
            if isinstance(pj, dict) and isinstance(pj.get("items"), list):
                out.append((f"语义面 pii 评审 score {_fmt(pj.get('score'))}",
                            ["检查点", "结果", "原文引用"], _judge_rows(pj["items"])))
    if not out and allow_generic:
        out = _flatten_table(data)
    return out


def _overview_html(report: dict) -> str:
    """顶部 16 行总览（结果栏：✓/✗/⚠/–）——一眼扫完，不用十几个卡片里翻（ADR-0030）。"""
    rows = []
    for k in ALL_KEYS:
        v = report["metrics"][k]
        rows.append(f"<tr><td>{k}</td><td>{_esc(DIM_NAMES.get(k, ''))}</td><td>{_glyph(v['verdict'])}</td>"
                    f"<td>{_esc(v['method'])}</td><td>{_esc(_cell(v.get('note') or ''))}</td></tr>")
    return ("<div class='card'><h3 style='margin:0'>16 项指标总览</h3>"
            "<table id='overview'><tr><th>指标</th><th>名称</th><th>结果</th><th>手段</th><th>摘要</th></tr>"
            + "".join(rows) + "</table></div>")


def _advice_html(advice: list) -> str:
    """「优化建议」tab：按严重度排序的汇总表，每项带 #N 锚点跳回指标卡。"""
    if not advice:
        return "<p class='hint'>16 项指标全部通过，无待办建议。</p>"
    rank = {}
    for a in advice:
        rank.setdefault(a["severity"], 0)
        rank[a["severity"]] += 1
    head = "<p class='hint'>" + "、".join(
        f"{name} {rank.get(sev, 0)} 条" for sev, name in
        (("fail", "未通过"), ("warn", "警告"), ("info", "未测")) if rank.get(sev)) + "</p>"
    items = []
    for a in advice:
        cls = ADV_SEV_CLS.get(a["severity"], "skipped")
        items.append(
            f"<div class='card adv-item' data-metric='{_esc(a['metric'])}'>"
            f"<div><a href='#m-{a['metric'].lstrip('#')}'>{_esc(a['metric'])}</a> "
            f"<span class='vf vf-{cls}'>{VF_GLYPH.get(cls, '–')}</span> "
            f"<strong>{_esc(a['problem'])}</strong></div>"
            f"<div class='hint'>证据：{_esc(a['evidence'])}</div>"
            f"<div>建议：{_esc(a['action'])}</div></div>")
    return head + "".join(items)


def _judge_summary(data) -> str:
    """LLM 评审面摘要：score + reason（旧 HTML 只渲染 items，丢了 evidence/reason）。"""
    bits = []
    score = data.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        bits.append(f"score {_fmt(score)}")
    if data.get("reason"):
        bits.append(str(data["reason"]))
    return " · ".join(bits)


#16 条指标的结论符号（ADR-0030）：网页用 ✓/✗，结论类名仍走原三档样式
CONCLUSION_CLS = {"all-pass": "pass", "with-warnings": "warn", "has-failures": "fail"}


def render_html(report: dict, evalset_dir=None, skill_arg=None) -> str:
    # 按 ID 顺序 + 中文指标名；#43：SVG 流程图；ADR-0030：表格优先 + 建议内联 + ✓/✗ 结果栏
    advice = report.get("advice") or []
    by_metric = {}
    for a in advice:
        by_metric.setdefault(a["metric"], []).append(a)
    cards = [_overview_html(report)]
    for k in ALL_KEYS:
        v = report["metrics"][k]
        data = v.get("data")
        items_html = ""
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            summary = _judge_summary(data)
            trs = "".join(
                f"<tr><td>{_esc(i['name'])}</td><td>{_glyph('pass' if i['pass'] else 'fail')}</td>"
                f"<td><code>{_esc(i['quote'][:80])}</code></td></tr>"
                for i in data["items"])
            items_html = ((f"<p style='margin:6px 0 2px;font-size:13px;color:#475569'>{_esc(summary)}</p>"
                           if summary else "")
                          + f"<table><tr><th>检查点</th><th>结果</th><th>原文引用</th></tr>{trs}</table>")
        # 表格优先（ADR-0030）：定制表 → 通用兜底；已用 items 渲染出表时不再把同一份 dict 重复压平
        tables = _tables(k, data, allow_generic=not items_html) if data is not None else []
        table_html = "".join(_html_table(*t) for t in tables)
        # 原始 JSON 一律可见：有表就折叠（表格是视图，折叠里是原文），不静默截断
        raw_html = _raw_html(data, folded=bool(items_html or tables)) if data is not None else ""
        adv_html = ""
        for a in by_metric.get(k, []):
            cls = ADV_SEV_CLS.get(a["severity"], "skipped")
            adv_html += (f"<div class='adv'><span class='vf vf-{cls}'>{VF_GLYPH.get(cls, '–')}</span> "
                         f"<strong>{_esc(a['problem'])}</strong> → {_esc(a['action'])}"
                         f"<div class='hint'>证据：{_esc(a['evidence'])}</div></div>")
        if adv_html:
            adv_html = "<h4 style='margin:12px 0 4px'>优化建议</h4>" + adv_html
        cards.append(
            f"<div class='card' id='m-{k.lstrip('#')}'><h3 style='margin:0'>{k} · "
            f"{_esc(DIM_NAMES.get(k, ''))} {_badge(v['verdict'])}"
            f"<small style='color:#64748b'>（{_esc(v['method'])}）</small></h3>"
            f"<p style='font-size:13px;color:#475569'>{_esc(v.get('note') or '')}</p>"
            f"{table_html}{items_html}{adv_html}{raw_html}</div>")
    cls = report["conclusion"]
    meta_bits = []
    if report.get("tier"):
        meta_bits.append(f"<span class='badge skipped' style='margin-left:8px'>{_esc(report['tier'])}</span>")
    if report.get("reviewed_by") == "ai":
        meta_bits.append("<small style='margin-left:8px;color:#fbbf24'>评测集 AI 审核，未经人工复核</small>")
    return (_HTML_TMPL.replace("__CLS__", CONCLUSION_CLS.get(cls, "skipped"))
            .replace("__CONCLUSION__", _esc(report["conclusion"]))
            .replace("__META__", "" .join(meta_bits))
            .replace("__CARDS__", "".join(cards))
            .replace("__ADVICE__", _advice_html(report.get("advice") or []))
            .replace("__EVALSET__", _evalset_html(evalset_dir, _skill_meta(skill_arg, evalset_dir)))
            .replace("__FLOW__", _flow_svg()))


def render_md(report: dict) -> str:
    head = ["# Skill 评估报告", "", f"**结论：{report['conclusion']}**"]
    if report.get("tier"):
        head.append(f"**档位：{report['tier']}**")
    if report.get("reviewed_by") == "ai":
        head.append("**评测集 AI 审核，未经人工复核**")
    ev = report.get("evaluator") or {}
    head.append(f"**评估器指纹：{ev.get('version', 'unknown')}**（{ev.get('files', 0)} 个文件的内容 sha256）")
    lines = head + [""]
    for k in ALL_KEYS:  # 严格按指标 ID 顺序（ADR-0032）
        v = report["metrics"][k]
        note = f" — {v['note']}" if v.get("note") else ""
        name = DIM_NAMES.get(k, "")
        method = {METHOD_STATIC: "静态检查", METHOD_RUN: "沙箱运行",
                  METHOD_JUDGE: "LLM评审", METHOD_COMPARE: "对比"}.get(v["method"], v["method"])
        # #20：中文指标名 + 中文测量手段；data 渲染：一律表格化（ADR-0030），report.json 仍是权威
        lines.append(f"### {k} · {name} [{v['verdict']}]（{method}）{note}")
        data = v.get("data")
        if data is not None:
            has_items = isinstance(data, dict) and isinstance(data.get("items"), list)
            if has_items:
                lines += _md_table(_judge_summary(data), ["检查点", "结果", "原文引用"],
                                   [[i["name"], "✓" if i["pass"] else "✗", i["quote"]]
                                    for i in data["items"]])
            for tbl in _tables(k, data, allow_generic=not has_items):
                lines += _md_table(*tbl)
        lines.append("")
    # 优化建议（ADR-0030）：与指标裁决同源，每条带 #N 反链
    lines += ["## 优化建议", ""]
    advice = report.get("advice") or []
    if not advice:
        lines.append("- 16 项指标全部通过，无待办建议。")
    for a in advice:
        lines.append(f"- **{a['metric']}**（{a['severity']}）{a['problem']} → {a['action']}")
        if a.get("evidence"):
            lines.append(f"  - 证据：{a['evidence']}")
    lines.append("")
    return "\n".join(lines)


def main():
    args = sys.argv[1:]
    html = evalset_dir = skill_arg = tier = None
    rest = []
    i = 0
    while i < len(args):
        if args[i] == "--html":
            html = True; i += 1
        elif args[i] == "--evalset":
            evalset_dir = Path(args[i + 1]); i += 2
        elif args[i] == "--skill":
            skill_arg = Path(args[i + 1]); i += 2
        elif args[i] == "--tier":
            tier = args[i + 1]; i += 2
        else:
            rest.append(args[i]); i += 1
    if len(rest) != 1:
        print("usage: report.py <results目录> [--tier <static|review|trigger|core|full>] "
              "[--html] [--evalset <evalsets/<name>/vN>] [--skill <被测skill目录>]", file=sys.stderr)
        sys.exit(2)
    d = Path(rest[0])
    if not d.is_dir():
        print(f"results 目录不存在: {d}", file=sys.stderr)
        sys.exit(2)
    report = build(d, evalset_dir)
    if tier:
        report["tier"] = tier
    meta = build_meta(report, skill_arg, evalset_dir)
    _console.write_text(d / "report.json", json.dumps(report, ensure_ascii=False, indent=2))
    _console.write_text(d / "report.md", render_md(report))
    write_meta(d, meta)
    if html:
        _console.write_text(d / "report.html", render_html(report, evalset_dir, skill_arg))
    print(json.dumps({"conclusion": report["conclusion"], "written": True,
                      "meta_schema": META_SCHEMA,
                      "evaluator": report["evaluator"]["version"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
