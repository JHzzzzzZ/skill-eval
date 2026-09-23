"""报告生成（17/18 的自我应用：固定输出契约）。
Seam: python report.py <results目录> [--html] [--evalset <evalsets/<name>/vN>] [--skill <被测skill目录>]

读同目录中间产物（static.json/score.json/idem.json/compare.json/golden.json/evalset.json/
model_robust.json/judges/*.json），产出 report.json（19 key，机器可读）+ report.md（人可读）
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

F1_PASS = 0.7  # 触发评测及格线（可配）
COST_CV_MAX = 0.5  # #12 稳定性阈值：tool_calls 变异系数上限（可配）
META_SCHEMA = "skill-eval/results-meta/1"  # 与 uploads/、evalsets/ 下的同名 meta.json 靠此字段区分
EVALUATOR_ROOT = Path(__file__).resolve().parent.parent  # 评估器自身目录（指纹范围）

# #19/#20：每个指标的中文含义（report.md 与 report.json 的 note 均引用，不能只写 #N）
DIM_NAMES = {
    "#1": "触发精准度", "#2": "name/description 规范与 token 预算", "#3": "正文精简",
    "#4": "运行成本", "#5": "必要性 A/B", "#6": "低冗余", "#7": "调用方式",
    "#8": "最小依赖", "#9": "结果可验证", "#10": "过程可验证", "#11": "fallback",
    "#12": "稳定性", "#13": "权限最小化", "#14": "版本演进", "#15": "幂等可恢复",
    "#16": "前置自检", "#17": "输入契约", "#18": "输出契约", "#19": "副作用可逆",
}

# #13 的五个静态规则组（ADR-0013）：用于 note 里的命中摘要
DIM_13_GROUPS = {"dangerous": "危险命令", "secrets": "硬编码凭据",
                 "injection": "注入指令", "exfil": "数据外发", "obfuscation": "混淆"}

ALL_KEYS = [f"#{i}" for i in range(1, 20)]
THREE_TIER = {"pass", "warn", "fail", "skipped"}
METHOD_STATIC = "静态检查"
METHOD_RUN = "沙箱运行"
METHOD_DIFF = "历史对比"  # #14 专属：evolution.py 读历史 report.json，不碰沙箱
METHOD_JUDGE = "LLM评审"
METHOD_COMPARE = "对比"


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
    """评估器指纹（ADR-0010）：scripts/*.py + judges/*.md + SKILL.md + reference.md 的内容 sha256。

    用途：results/meta.json 与 report.json 记录"这份报告由哪个评估器产生"；#14 跨版本对比时
    评估器换过版 → 指标差异不再构成被测 skill 的回归证据。
    用内容指纹而非人手 semver：零维护、无法忘记更新、可复现。
    """
    root = Path(root or EVALUATOR_ROOT)
    try:
        files = sorted([p for p in list((root / "scripts").glob("*.py"))
                        + list((root / "judges").glob("*.md"))
                        + [root / "SKILL.md", root / "reference.md"] if p.is_file()],
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
    (d / "meta.json").write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")


def verdict_of_ratio(ratio) -> str:
    """#5：LLM 评审分数是 pass 项数/总项数（0~1），全过 pass、有挂 warn、全挂 fail。"""
    return "pass" if ratio >= 1 else "warn" if ratio > 0 else "fail"


def verdict_of_score(score) -> str:
    return {0: "fail", 1: "warn", 2: "pass"}.get(score, "skipped")


def from_static(static, m: dict):
    if not isinstance(static, dict):
        return
    errors = static.get("errors", [])
    invoke_warn = any("disable-model-invocation" in w for w in static.get("warnings", []))
    m_err = any("name" in e or "description" in e for e in errors)
    token_warn = any("token" in w for w in static.get("warnings", []))
    m["#2"] = {"verdict": "fail" if m_err else "warn" if token_warn else "pass",
               "method": METHOD_STATIC,
               "data": {"description_tokens": static.get("description_tokens")},
               "note": "name/description 规范与 token 阈值"}
    m["#7"] = {"verdict": "warn" if invoke_warn
               else "pass" if static.get("invoke", {}).get("resolved") in ("human", "both")
               else "skipped",
               "method": METHOD_STATIC, "data": static.get("invoke"),
               "note": "调用方式 frontmatter 校验（disable-model-invocation：true=human，缺省=both）"}
    # #13 权限最小化（ADR-0013）：五组静态规则。四组命中即 fail，混淆仅 warn（待人工复核）。
    # 旧的 static.json（无四组新字段）走同一分支：get→None→[]，verdict 与旧版一致。
    hard_groups = ("dangerous", "secrets", "injection", "exfil")
    groups = {k: (static.get(k) or []) for k in (*hard_groups, "obfuscation")}
    hit = [k for k in hard_groups if groups[k]]
    note = "五组静态规则（危险命令/凭据/注入/外发/混淆）"
    if hit:
        note += "；命中：" + "、".join(f"{DIM_13_GROUPS.get(k, k)}×{len(groups[k])}" for k in hit)
    elif groups["obfuscation"]:
        note += f"；仅混淆×{len(groups['obfuscation'])}（警告）"
    # 排除/抑制必须可见（ADR-0012）：不静默丢指标
    excluded, ignored = static.get("excluded") or [], static.get("ignored") or []
    if excluded:
        shown = "、".join(excluded[:3]) + ("…" if len(excluded) > 3 else "")
        note += f"；未扫描 {len(excluded)} 处（{shown}）"
    if ignored:
        note += f"；行内抑制 {len(ignored)} 行"
    m["#13"] = {"verdict": "fail" if hit else "warn" if groups["obfuscation"] else "pass",
                "method": METHOD_STATIC, "data": groups, "note": note}


def cv_of(comp):
    """变异系数（#12，ADR-0016）：std/mean；n<2 或字段不合法 → None（不参与裁决）。

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


def build(d: Path, evalset_dir: Path | None = None) -> dict:
    m = {}
    static = load(d, "static.json")
    score = load(d, "score.json")
    idem = load(d, "idem.json")
    compare = load(d, "compare.json")
    golden = load(d, "golden.json")

    # 静态组
    for k in ("#2", "#7", "#13"):
        m[k] = {"verdict": "skipped", "method": METHOD_STATIC, "data": None, "note": "无 static.json"}
    if isinstance(static, dict):
        from_static(static, m)

    # 运行组
    m["#1"] = {"verdict": "skipped", "method": METHOD_RUN, "data": None, "note": "无触发评测数据"}
    m["#4"] = {"verdict": "skipped", "method": METHOD_RUN, "data": None, "note": "无成本数据"}
    m["#5"] = {"verdict": "skipped", "method": METHOD_RUN, "data": None, "note": "无基线数据"}
    m["#8"] = {"verdict": "skipped", "method": METHOD_RUN, "data": None, "note": "无 golden trace"}
    m["#12"] = {"verdict": "skipped", "method": METHOD_RUN, "data": None, "note": "无多次运行数据"}
    if isinstance(score, dict):
        t = score.get("trigger")
        if isinstance(t, dict):
            m["#1"] = {"verdict": "pass" if t.get("f1", 0) >= F1_PASS else "fail",
                       "method": METHOD_RUN, "data": t, "note": f"F1 及格线 {F1_PASS}"}
        cost = score.get("cost")
        if isinstance(cost, dict) and any(v != "skipped" for v in cost.values()):
            m["#4"] = {"verdict": "pass", "method": METHOD_RUN, "data": cost, "note": "单次运行成本统计"}
        n = score.get("necessity")
        if isinstance(n, dict):
            tok = n.get("tokens") if isinstance(n.get("tokens"), dict) else None
            m["#5"] = {"verdict": "pass" if (tok or {}).get("improvement", 0) > 0 else "fail",
                       "method": METHOD_RUN, "data": n,
                       "note": "三分量对比（#11）：token 为主判据，步数/耗时并列呈现"}
    # #1 人调用型（#7 resolved=human）不适用触发评测：pi 的 formatSkillsForPrompt 会把
    # disable-model-invocation=true 的 skill 从提示中滤除（实测：--skill 加载后模型答
    # “无 available_skills 段”）——“按 description 触发”在该载体上结构上不可能，照跑只是把
    # 载体限制记成 skill 的 fail。已实测的数据不丢，原样留在 data 里作证据，但不改判。
    if isinstance(static, dict) and (static.get("invoke") or {}).get("resolved") == "human":
        m["#1"] = {"verdict": "skipped", "method": METHOD_RUN, "data": m["#1"]["data"],
                   "note": ("调用方式为 human（disable-model-invocation=true）：不跑触发评测，"
                            "本指标不适用（SKILL.md 未声明触发式调用）")}
    # #1 触发集质量（ADR-0002 的执行检查）：照抄 description 的 prompt 会让 P/R 虚高 → 降级为 warn，
    # 不做整体闸门（与 ADR-0008 的区别：那是客观硬事实，这里是启发式阈值，疑似不该一票否决）
    ev = load(d, "evalset.json")
    if isinstance(ev, dict) and ev.get("clean") is False and m["#1"]["verdict"] != "skipped":
        m["#1"]["verdict"] = "fail" if m["#1"]["verdict"] == "fail" else "warn"
        m["#1"]["data"] = {**(m["#1"]["data"] or {}), "evalset_quality": ev}
        m["#1"]["note"] = (m["#1"].get("note") or "") + f"；触发集质量警告：{ev.get('note')}"

    if isinstance(golden, dict):
        errs = [e for e in golden.get("errors", []) if any(
            w in str(e).lower() for w in ("not found", "notfound", "no such", "import", "command not found", "不存在"))]
        # #8 双源（#29/#30）：trace 报错（实跑面）+ judges/deps.json（语义面）合成裁决
        deps = load_judge(d, "deps")
        if errs:
            dep_verdict, dep_note = "fail", "golden trace 中有依赖类报错"
        elif deps:
            dep_verdict = verdict_of_ratio(deps["score"])
            dep_note = "无依赖类报错，语义评审最小依赖（judges/deps.md，pass 项数/总项数）"
        else:
            dep_verdict, dep_note = "pass", "golden trace 中无依赖类报错即 pass"
        m["#8"] = {"verdict": dep_verdict, "method": METHOD_RUN,
                   "data": {"dependency_errors": errs,
                            "deps_judge": {"score": deps["score"], "items": deps.get("items")} if deps else None},
                   "note": dep_note}
    if isinstance(score, dict) and isinstance(score.get("cost"), dict) \
            and isinstance(score["cost"].get("tool_calls"), dict):
        # #12（ADR-0016）：裁决用**组内（逐 case）** 变异系数中位数——
        # pooled 会把 case 间的难度差算成“不稳定”（实测：组间均值极差 8.5 步 → pooled cv 0.65，
        # 组内中位数 0.45）。无逐 case 数据（旧产物/旧 harness）时回退 pooled 并在 note 标注口径。
        cost_data = score["cost"]
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
            if med <= COST_CV_MAX:
                verdict = "pass"
                note = (f"组内（逐 case）变异系数中位数 {med:.2f} ≤ {COST_CV_MAX}，稳定"
                        f"（最大 {cv_cases[worst]:.2f} @ {worst}）{tail}")
            else:
                verdict = "warn"
                note = (f"组内（逐 case）变异系数中位数 {med:.2f} 超过阈值 {COST_CV_MAX}，不稳定"
                        f"（最大 {cv_cases[worst]:.2f} @ {worst}）{tail}")
            data = {"cv_scope": "within-case", "cv_threshold": COST_CV_MAX,
                    "cv_median": round(med, 3), "cv_max": round(cv_cases[worst], 3),
                    "cv_max_case": worst, "cv_by_case": {c: round(v, 3) for c, v in cv_cases.items()},
                    "cv_pooled": None if pooled_cv is None else round(pooled_cv, 3),
                    "cost_by_case": raw_cases, "cost": cost_data}
        else:
            stable = pooled_cv is not None and pooled_cv <= COST_CV_MAX
            shown = 0.0 if pooled_cv is None else pooled_cv
            note = (f"变异系数 {shown:.2f} ≤ {COST_CV_MAX}，稳定（pooled 口径：无逐 case 数据）"
                    if stable else
                    f"变异系数 {shown:.2f} > {COST_CV_MAX}，不稳定（pooled 口径：无逐 case 数据）")
            verdict = "pass" if stable else "warn"
            data = {"cv_scope": "pooled", "cv_threshold": COST_CV_MAX,
                    "cv_pooled": None if pooled_cv is None else round(pooled_cv, 3), "cost": cost_data}
        m["#12"] = {"verdict": verdict, "method": METHOD_RUN, "data": data, "note": note}

    # #12 跨模型一致性（ADR-0011：折进稳定性，不新增第 20 指标）：
    # 组内变异系数与跨模型一致率分写在同一 data 下，不混成一个数字。
    # 保守口径：跨模型只能**降级**（发现漂移），不能升级——它不能代替组内重复运行。
    mr = load(d, "model_robust.json")
    if isinstance(mr, dict) and mr.get("skipped") is False and mr.get("robust") is not None:
        rank = {"skipped": 0, "pass": 1, "warn": 2, "fail": 3}
        cross = "pass" if mr["robust"] else "warn"
        m["#12"] = {"verdict": max((m["#12"]["verdict"], cross), key=lambda v: rank.get(v, 0)),
                    "method": METHOD_RUN,
                    "data": {"intra_model_cost": m["#12"].get("data"), "model_robust": mr},
                    "note": (m["#12"].get("note") or "") + f"；跨模型一致性：{mr.get('note')}"}

    m["#9"] = {"verdict": "skipped", "method": METHOD_COMPARE, "data": None, "note": "无 expect/actual 对比"}
    if isinstance(compare, dict) and (compare.get("score") is not None
                                      or compare.get("mean_score") is not None):
        m["#9"] = {"verdict": "pass" if compare.get("match") else "fail",
                   "method": METHOD_COMPARE, "data": compare,
                   "note": "期望输出 vs 实际产出（聚合口径：多数 case 匹配即 pass）"}

    m["#15"] = {"verdict": "skipped", "method": METHOD_RUN, "data": None, "note": "无幂等数据"}
    if isinstance(idem, dict) and "idempotent" in idem:
        m["#15"] = {"verdict": "pass" if idem["idempotent"] else "fail",
                    "method": METHOD_RUN, "data": idem, "note": "重复步骤比例 ≤ 0.5 即幂等"}

    # LLM 评审组
    judges = {"#3": "brevity", "#6": "redundancy", "#11": "fallback", "#16": "precheck",
              "#17": "contract", "#18": "contract", "#19": "side-effects"}
    for key, name in judges.items():
        j = load_judge(d, name)
        m[key] = {"verdict": verdict_of_ratio(j["score"]) if j else "skipped",
                  "method": METHOD_JUDGE,
                  "data": ({"score": j["score"], "items": j.get("items"),
                            "evidence": j["evidence"], "reason": j["reason"]} if j else None),
                  "note": (name + "（逐项计分：" + "/".join(
                      f"{i['name']}={'✓' if i['pass'] else '✗'}" for i in j.get("items", [])) + "）")
                  if j else "无评审输出"}

    # #3 双源（ADR-0017）：正文行数 = 可数事实 → 静态面主源（超线即 warn，机械证据优先）；
    # 语义两项 → LLM 面辅证，结果原形状保留在同一份 data 里，不丢。
    if isinstance(static, dict):
        lines, limit = static.get("skill_md_body_lines"), static.get("skill_md_body_max_lines")
        if isinstance(lines, int) and isinstance(limit, int):
            cur = m["#3"]
            merged = {"skill_md_body_lines": lines, "skill_md_body_max_lines": limit}
            if isinstance(cur.get("data"), dict):
                merged.update(cur["data"])
            if lines > limit:
                m["#3"] = {"verdict": "warn", "method": METHOD_STATIC, "data": merged,
                           "note": f"正文 {lines} 行 > 阈值 {limit} 行（静态面主源，ADR-0017）；"
                                   + (cur.get("note") or "无语义评审输出")}
            elif isinstance(cur.get("data"), dict):
                m["#3"] = {**cur, "data": merged,
                           "note": (cur.get("note") or "")
                                   + f"；正文 {lines} 行 ≤ 阈值 {limit}（静态面）"}
            else:
                m["#3"] = {"verdict": "skipped", "method": METHOD_STATIC, "data": merged,
                           "note": f"正文 {lines} 行 ≤ 阈值 {limit}（静态面通过）；无语义评审输出"}

    m["#10"] = {"verdict": "skipped", "method": METHOD_RUN, "data": None, "note": "无 process.json"}
    proc = load(d, "process.json")
    if isinstance(proc, dict) and isinstance(proc.get("score"), int):
        m["#10"] = {"verdict": verdict_of_score(proc["score"]), "method": METHOD_RUN,
                    "data": proc, "note": "trace 对照声明的过程审计"}
    ab = load(d, "ablation.json")
    if isinstance(ab, dict) and ab.get("f1_full") is not None:
        m["#6"] = {"verdict": "pass" if ab.get("f1_ablated", 0) < ab.get("f1_full", 1) else "warn",
                   "method": METHOD_RUN, "data": ab,
                   "note": "消融后掉分=原文必要(通过)；持平/上升=冗余实证(警告)"}
    m["#14"] = {"verdict": "skipped", "method": METHOD_DIFF, "data": None, "note": "evolution.py 独立产出"}

    # 评测集 AI 审核（#49）：reviewed_by=ai 时给依赖评测集的指标加标注
    reviewed_by = _evalset_reviewed_by(d, evalset_dir)
    if reviewed_by == "ai":
        ai_note = "评测集 AI 审核，未经人工复核"
        for k in ("#1", "#5", "#9"):
            if m[k]["verdict"] != "skipped":
                m[k]["note"] = f"{m[k]['note']}（{ai_note}）"

    from datetime import datetime
    report = {"metrics": m, "conclusion": conclusion(m),
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
    ("3 静态检查", "static_check.py：#2 规范、#7 调用方式、#13 五组规则、可移植性闸门"),
    ("4 评测集", "检查冻结的 evalsets/<name>/vN/；未冻结则生成 → 条数闸门 + 触发集质量自检 → LLM 自动审核（换模型）即冻结（reviewed_by=ai）"),
    ("5 沙箱运行", "按档增量跑 pi CLI：T2 触发（early-exit）→ T3 主运行+基线+重复（可选跨模型 model_robust.py）→ T4 审计/消融"),
    ("6 LLM 评审", "judges/ 逐项计分 rubric，judge_runner.py --validate 通过才进报告"),
    ("7 计算", "score.py（P/R/F1、成本、三分量必要性）+ idem.py（幂等）"),
    ("8 报告", "report.py：report.json + report.md（--html 加静态页），19 指标恒出现"),
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
<button data-t="evalset" onclick="tab('evalset')">评测集审核</button>
<button data-t="flow" onclick="tab('flow')">评估流程</button></nav>
<main>
<section class="tab on" id="t-report">__CARDS__</section>
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


DATA_INLINE_MAX = 400  # 无表可渲染时的内联阈值（字符）；超过则折叠
CELL_MAX = 800  # 表格单元格截断阈值（字符）；全文在折叠的原始 data 里
# data 渲染口径（见 reference.md § 报告契约）：表格优先 → 原始 JSON 折叠；短 data 内联；**不静默截断**
# （旧实现 [:2000] 会把 #15 这种 2165 字符的 data 切成非法 JSON）


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
    """表格单元格文本：压平空白；超 CELL_MAX 截断（全文仍在折叠的原始 data 里）。"""
    if v is None:
        return ""
    text = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
    text = " ".join(text.split())
    return text if len(text) <= CELL_MAX else text[:CELL_MAX] + "…（截断，全文见原始 data）"


def _md_cell(v) -> str:
    return _cell(v).replace("|", "\\|")


def _data_table(metric: str, data):
    """已知形状的 data → (摘要行, 列名, 行)；无表可渲染返回 None。

    只覆盖“同构行数组”型 data（#15/#12/#4/#9），形状与 report.json 同源，
    渲染层不得反过来改 report.json 的形状。"""
    if not isinstance(data, dict):
        return None
    if metric == "#15":
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
    if metric == "#12":
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
    if metric == "#4":
        labels = {"tool_calls": "工具步数", "tokens": "tokens", "seconds": "秒"}
        rows = [[labels[k]] + [_fmt((data.get(k) or {}).get(x)) for x in ("mean", "std", "n", "min", "max")]
                for k in ("tool_calls", "tokens", "seconds") if isinstance(data.get(k), dict)]
        if not rows:
            return None
        return f"单次运行成本（{len(rows)} 个分量，pooled）", ["分量", "均值", "标准差", "n", "最小", "最大"], rows
    if metric == "#9":
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


def _raw_html(data) -> str:
    """原始 data：短的内联，长的一律折叠——不再静默截断（旧实现 [:2000] 会切断 JSON）。"""
    raw = json.dumps(data, ensure_ascii=False, indent=2)
    if len(raw) <= DATA_INLINE_MAX:
        return f"<pre>{_esc(raw)}</pre>"
    return (f"<details><summary>原始 data（{len(raw)} 字符，与 report.json 同源）</summary>"
            f"<pre>{_esc(raw)}</pre></details>")


def _judge_summary(data) -> str:
    """LLM 评审面摘要：score + reason（旧 HTML 只渲染 items，丢了 evidence/reason）。"""
    bits = []
    score = data.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        bits.append(f"score {_fmt(score)}")
    if data.get("reason"):
        bits.append(str(data["reason"]))
    return " · ".join(bits)


def render_html(report: dict, evalset_dir=None, skill_arg=None) -> str:
    # #19/#20：按 ID 顺序 + 中文指标名；#38：评测集审核同页；#43：SVG 流程图
    cards = []
    for k in ALL_KEYS:
        v = report["metrics"][k]
        data = v.get("data")
        items_html = ""
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            summary = _judge_summary(data)
            trs = "".join(
                f"<tr><td>{_esc(i['name'])}</td><td>{'✓' if i['pass'] else '✗'}</td>"
                f"<td><code>{_esc(i['quote'][:80])}</code></td></tr>"
                for i in data["items"])
            items_html = ((f"<p style='margin:6px 0 2px;font-size:13px;color:#475569'>{_esc(summary)}</p>"
                           if summary else "")
                          + f"<table><tr><th>检查点</th><th>结果</th><th>原文引用</th></tr>{trs}</table>")
        table_html = ""
        if data is not None and not items_html:
            tbl = _data_table(k, data)
            if tbl:
                table_html = _html_table(*tbl)
        # 原始 data 一律可见（表格是视图，折叠里是原文）：report.html 不得比 report.md 少信息
        raw_html = _raw_html(data) if data is not None else ""
        cards.append(
            f"<div class='card'><h3 style='margin:0'>{k} · {_esc(DIM_NAMES.get(k, ''))} "
            f"<span class='badge {v['verdict']}'>{v['verdict']}</span>"
            f"<small style='color:#64748b'>（{_esc(v['method'])}）</small></h3>"
            f"<p style='font-size:13px;color:#475569'>{_esc(v.get('note') or '')}</p>"
            f"{items_html}{table_html}{raw_html}</div>")
    cls = report["conclusion"]
    meta_bits = []
    if report.get("tier"):
        meta_bits.append(f"<span class='badge skipped' style='margin-left:8px'>{_esc(report['tier'])}</span>")
    if report.get("reviewed_by") == "ai":
        meta_bits.append("<small style='margin-left:8px;color:#fbbf24'>评测集 AI 审核，未经人工复核</small>")
    return (_HTML_TMPL.replace("__CLS__", cls)
            .replace("__CONCLUSION__", _esc(report["conclusion"]))
            .replace("__META__", "" .join(meta_bits))
            .replace("__CARDS__", "".join(cards))
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
    for k in ALL_KEYS:  # #19：严格按指标 ID 顺序
        v = report["metrics"][k]
        note = f" — {v['note']}" if v.get("note") else ""
        name = DIM_NAMES.get(k, "")
        method = {METHOD_STATIC: "静态检查", METHOD_RUN: "沙箱运行",
                  METHOD_JUDGE: "LLM评审", METHOD_COMPARE: "对比",
                  METHOD_DIFF: "历史对比"}.get(v["method"], v["method"])
        # #20：中文指标名 + 中文测量手段；data 渲染：表格优先，长 data 只留指针（report.json 是权威）
        lines.append(f"### {k} · {name} [{v['verdict']}]（{method}）{note}")
        data = v.get("data")
        if data is not None:
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                lines += _md_table(_judge_summary(data), ["检查点", "结果", "原文引用"],
                                   [[i["name"], "✓" if i["pass"] else "✗", i["quote"]] for i in data["items"]])
            else:
                tbl = _data_table(k, data)
                if tbl:
                    lines += _md_table(*tbl)
                else:
                    raw = json.dumps(data, ensure_ascii=False, indent=2)
                    lines.append(f"```json\n{raw}\n```" if len(raw) <= DATA_INLINE_MAX
                                 else f"完整 data 见 `report.json`（{len(raw)} 字符）")
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
    (d / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "report.md").write_text(render_md(report), encoding="utf-8")
    write_meta(d, meta)
    if html:
        (d / "report.html").write_text(render_html(report, evalset_dir, skill_arg), encoding="utf-8")
    print(json.dumps({"conclusion": report["conclusion"], "written": True,
                      "meta_schema": META_SCHEMA,
                      "evaluator": report["evaluator"]["version"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
