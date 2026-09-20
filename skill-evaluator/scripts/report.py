"""报告生成（17/18 的自我应用：固定输出契约）。Seam: python report.py <results目录>

读同目录中间产物（static.json/score.json/idem.json/compare.json/golden.json/judges/*.json），
产出 report.json（19 key，机器可读）+ report.md（人可读）。契约见 tests/test_report.py 模块注释。
"""
import json
import sys
from pathlib import Path

F1_PASS = 0.7  # 触发评测及格线（可配）
COST_CV_MAX = 0.5  # #12 稳定性阈值：tool_calls 变异系数上限（可配）

# #19/#20：每个指标的中文含义（report.md 与 report.json 的 note 均引用，不能只写 #N）
DIM_NAMES = {
    "#1": "触发精准度", "#2": "name/description 规范与 token 预算", "#3": "正文精简",
    "#4": "运行成本", "#5": "必要性 A/B", "#6": "低冗余", "#7": "调用方式",
    "#8": "最小依赖", "#9": "结果可验证", "#10": "过程可验证", "#11": "fallback",
    "#12": "稳定性", "#13": "权限最小化", "#14": "版本演进", "#15": "幂等可恢复",
    "#16": "前置自检", "#17": "输入契约", "#18": "输出契约", "#19": "副作用可逆",
}

ALL_KEYS = [f"#{i}" for i in range(1, 20)]
THREE_TIER = {"pass", "warn", "fail", "skipped"}
METHOD_STATIC = "静态检查"
METHOD_RUN = "沙箱运行"
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


def verdict_of_ratio(ratio) -> str:
    """#5：LLM 评审分数是 pass 项数/总项数（0~1），全过 pass、有挂 warn、全挂 fail。"""
    return "pass" if ratio >= 1 else "warn" if ratio > 0 else "fail"


def verdict_of_score(score) -> str:
    return {0: "fail", 1: "warn", 2: "pass"}.get(score, "skipped")


def from_static(static, m: dict):
    if not isinstance(static, dict):
        return
    errors = static.get("errors", [])
    has_danger = bool(static.get("dangerous"))
    invoke_err = any("invoke" in e for e in errors)
    m_err = any("name" in e or "description" in e for e in errors)
    token_warn = any("token" in w for w in static.get("warnings", []))
    m["#2"] = {"verdict": "fail" if m_err else "warn" if token_warn else "pass",
               "method": METHOD_STATIC,
               "data": {"description_tokens": static.get("description_tokens")},
               "note": "name/description 规范与 token 阈值"}
    m["#7"] = {"verdict": "fail" if invoke_err
               else "pass" if static.get("invoke", {}).get("resolved") in ("human", "agent", "both")
               else "skipped",
               "method": METHOD_STATIC, "data": static.get("invoke"), "note": "调用方式 frontmatter 校验"}
    m["#13"] = {"verdict": "fail" if has_danger else "pass",
                "method": METHOD_STATIC, "data": static.get("dangerous"), "note": "危险命令扫描"}


def build(d: Path) -> dict:
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
        tc = score["cost"]["tool_calls"]
        n = tc.get("n", 1)
        mean, std = tc.get("mean", 0), tc.get("std", 0)  # score.py 已改输 std；兼容旧 var
        if "std" not in tc and "var" in tc:
            std = tc["var"] ** 0.5
        cv = (std / mean) if mean else (0 if std == 0 else 99)
        stable = n >= 2 and cv <= COST_CV_MAX
        m["#12"] = {"verdict": "pass" if stable else "warn", "method": METHOD_RUN,
                    "data": score["cost"],
                    "note": (f"变异系数 {cv:.2f} ≤ {COST_CV_MAX}，稳定" if n >= 2
                             else f"变异系数 {round(cv, 2)} > {COST_CV_MAX}，不稳定" if n >= 2
                             else "仅单次运行，无稳定性证据")}

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
    m["#14"] = {"verdict": "skipped", "method": METHOD_RUN, "data": None, "note": "evolution.py 独立产出"}

    from datetime import datetime
    report = {"metrics": m, "conclusion": conclusion(m),
              "generated_at": datetime.now().isoformat(timespec="seconds")}
    return report


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
    ("3 静态检查", "static_check.py：#2 规范、#7 调用方式、#13 危险命令、可移植性闸门"),
    ("4 评测集", "检查冻结的 evalsets/<name>/vN/；未冻结则生成后等人工审核"),
    ("5 沙箱运行", "git worktree 内实跑 pi CLI：触发（带 early-exit）+ 主运行 + 基线 A/B + 重复 ×N"),
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


def _evalset_html(evalset_dir) -> str:
    if not evalset_dir or not evalset_dir.is_dir():
        return "<p class='hint'>未指定评测集目录（--evalset），审核视图不可用。</p>"
    rows = []
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
            items.append(f"<li><code>{_esc(j.get('prompt', ''))}</code>"
                         + (f" → 期望：{_esc(expect)}" if expect else "") + "</li>")
        if items:
            rows.append(f"<h4>{group}（{len(items)}）</h4><ul>{''.join(items)}</ul>")
    return "".join(rows) or "<p class='hint'>评测集目录为空。</p>"


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
table{border-collapse:collapse;width:100%;font-size:13px}
td,th{border:1px solid #dde2e7;padding:4px 8px;text-align:left}
</style></head><body>
<header><h1 style="margin:0;font-size:20px">Skill 评估报告</h1>
<p style="margin:4px 0 0">结论：<span class="badge __CLS__">__CONCLUSION__</span></p></header>
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
 e=>e.classList.toggle('on',e.dataset.t===n));}</script>
</body></html>'''


def render_html(report: dict, evalset_dir=None) -> str:
    # #19/#20：按 ID 顺序 + 中文指标名；#38：评测集审核同页；#43：SVG 流程图
    cards = []
    for k in ALL_KEYS:
        v = report["metrics"][k]
        items_html = ""
        if isinstance(v.get("data"), dict) and isinstance(v["data"].get("items"), list):
            trs = "".join(
                f"<tr><td>{_esc(i['name'])}</td><td>{'✓' if i['pass'] else '✗'}</td>"
                f"<td><code>{_esc(i['quote'][:80])}</code></td></tr>"
                for i in v["data"]["items"])
            items_html = f"<table><tr><th>检查点</th><th>结果</th><th>原文引用</th></tr>{trs}</table>"
        data_html = ""
        if v.get("data") is not None and not items_html:
            data_html = f"<pre>{_esc(json.dumps(v['data'], ensure_ascii=False, indent=2)[:2000])}</pre>"
        cards.append(
            f"<div class='card'><h3 style='margin:0'>{k} · {_esc(DIM_NAMES.get(k, ''))} "
            f"<span class='badge {v['verdict']}'>{v['verdict']}</span>"
            f"<small style='color:#64748b'>（{_esc(v['method'])}）</small></h3>"
            f"<p style='font-size:13px;color:#475569'>{_esc(v.get('note') or '')}</p>"
            f"{items_html}{data_html}</div>")
    cls = report["conclusion"]
    return (_HTML_TMPL.replace("__CLS__", cls)
            .replace("__CONCLUSION__", _esc(report["conclusion"]))
            .replace("__CARDS__", "".join(cards))
            .replace("__EVALSET__", _evalset_html(evalset_dir))
            .replace("__FLOW__", _flow_svg()))


def render_md(report: dict) -> str:
    lines = ["# Skill 评估报告", "", f"**结论：{report['conclusion']}**", ""]
    for k in ALL_KEYS:  # #19：严格按指标 ID 顺序
        v = report["metrics"][k]
        note = f" — {v['note']}" if v.get("note") else ""
        name = DIM_NAMES.get(k, "")
        method = {METHOD_STATIC: "静态检查", METHOD_RUN: "沙箱运行",
                  METHOD_JUDGE: "LLM评审", METHOD_COMPARE: "对比"}.get(v["method"], v["method"])
        # #20：中文指标名 + 中文测量手段，data 保留原样供核对推导
        lines.append(f"### {k} · {name} [{v['verdict']}]（{method}）{note}")
        if v.get("data") is not None:
            lines.append(f"```json\n{json.dumps(v['data'], ensure_ascii=False, indent=2)}\n```")
        lines.append("")
    return "\n".join(lines)


def main():
    args = sys.argv[1:]
    html = evalset_dir = None
    rest = []
    i = 0
    while i < len(args):
        if args[i] == "--html":
            html = True; i += 1
        elif args[i] == "--evalset":
            evalset_dir = Path(args[i + 1]); i += 2
        else:
            rest.append(args[i]); i += 1
    if len(rest) != 1:
        print("usage: report.py <results目录> [--html] [--evalset <evalsets/<name>/vN>]", file=sys.stderr)
        sys.exit(2)
    d = Path(rest[0])
    if not d.is_dir():
        print(f"results 目录不存在: {d}", file=sys.stderr)
        sys.exit(2)
    report = build(d)
    (d / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "report.md").write_text(render_md(report), encoding="utf-8")
    if html:
        (d / "report.html").write_text(render_html(report, evalset_dir), encoding="utf-8")
    print(json.dumps({"conclusion": report["conclusion"], "written": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
