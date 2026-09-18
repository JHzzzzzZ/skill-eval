"""报告生成（17/18 的自我应用：固定输出契约）。Seam: python report.py <results目录>

读同目录中间产物（static.json/score.json/idem.json/compare.json/golden.json/judges/*.json），
产出 report.json（19 key，机器可读）+ report.md（人可读）。契约见 tests/test_report.py 模块注释。
"""
import json
import sys
from pathlib import Path

F1_PASS = 0.7  # 触发评测及格线（可配）
COST_CV_MAX = 0.5  # #12 稳定性阈值：tool_calls 变异系数上限（可配）

ALL_KEYS = [f"#{i}" for i in range(1, 20)]
THREE_TIER = {"pass", "warn", "fail", "skipped"}
METHOD_STATIC = "静态检查"
METHOD_RUN = "沙箱运行"
METHOD_JUDGE = "LLM评审"
METHOD_COMPARE = "对比"


def load(d: Path, name: str):
    try:
        return json.loads((d / name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_judge(d: Path, name: str):
    data = load(d, f"judges/{name}.json")
    if not isinstance(data, dict) or not isinstance(data.get("score"), int):
        return None
    return data


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
            m["#5"] = {"verdict": "pass" if n.get("improvement", 0) > 0 else "fail",
                       "method": METHOD_RUN, "data": n, "note": "token 提升率 > 0 即必要"}
    if isinstance(golden, dict):
        errs = [e for e in golden.get("errors", []) if any(
            w in str(e).lower() for w in ("not found", "no such", "import", "command not found", "不存在"))]
        m["#8"] = {"verdict": "fail" if errs else "pass", "method": METHOD_RUN,
                   "data": {"dependency_errors": errs}, "note": "golden trace 中无依赖类报错即 pass"}
    if isinstance(score, dict) and isinstance(score.get("cost"), dict) \
            and isinstance(score["cost"].get("tool_calls"), dict):
        tc = score["cost"]["tool_calls"]
        n = tc.get("n", 1)
        mean, var = tc.get("mean", 0), tc.get("var", 0)
        cv = (var ** 0.5 / mean) if mean else (0 if var == 0 else 99)
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
        m[key] = {"verdict": verdict_of_score(j["score"]) if j else "skipped",
                  "method": METHOD_JUDGE,
                  "data": {"score": j["score"], "evidence": j["evidence"], "reason": j["reason"]} if j else None,
                  "note": name if j else "无评审输出"}

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


def render_md(report: dict) -> str:
    lines = ["# Skill 评估报告", "", f"**结论：{report['conclusion']}**", ""]
    for k, v in report["metrics"].items():
        note = f" — {v['note']}" if v.get("note") else ""
        lines.append(f"### {k} [{v['verdict']}] ({v['method']}){note}")
        if v.get("data") is not None:
            lines.append(f"```json\n{json.dumps(v['data'], ensure_ascii=False, indent=2)}\n```")
        lines.append("")
    return "\n".join(lines)


def main():
    if len(sys.argv) != 2:
        print("usage: report.py <results目录>", file=sys.stderr)
        sys.exit(2)
    d = Path(sys.argv[1])
    if not d.is_dir():
        print(f"results 目录不存在: {d}", file=sys.stderr)
        sys.exit(2)
    report = build(d)
    (d / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "report.md").write_text(render_md(report), encoding="utf-8")
    print(json.dumps({"conclusion": report["conclusion"], "written": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
