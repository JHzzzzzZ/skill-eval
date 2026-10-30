"""报告可用性回归（用户六条需求 → ADR-0029/0030/0031）。

HTML 契约（ADR-0030，本文件的断言依据）：
- 每张指标卡 = `<div class='card' id='m-N'>…`（N 无 `#`，供建议锚点跳转）
- 徽章 = `<span class='badge vf-pass'>✓ pass</span>`（glyph + 原文）
- 总览表 = `<table id='overview'>`，16 行，首列 `<td>#N</td>`
- 原始 JSON：**有表就折叠**（`<details>`），无表且短才内联
- 建议：`report.json.advice` = [{metric, severity, problem, evidence, action}]；
  卡片内 `<div class='adv'>`；tab `id='t-advice'`；md 有 `## 优化建议`
覆盖：1 表格化 + 折叠 / 2 建议 / 3 闸门可见 / 4 ✓✗ / 5 依赖全面性 / 6 PII 评审折进 #4。
PII 与依赖全面性的静态面在 test_static_check.py。
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "report.py"
ALL_KEYS = [f"#{i}" for i in range(1, 17)]
JUDGES = ("brevity", "redundancy", "fallback", "precheck", "contract", "deps", "pii")
# #8 在"有数据"时的修复动作（与 skipped 时的补测动作必须不同，见 test_advice_skipped_gives_measurement_action）
ADVICE_FIX_ACTION_12 = ("补齐依赖声明（SKILL.md 依赖小节 + requirements.txt/check-deps.sh），"
                       "并复跑 golden trace")


def write_inputs(d: Path, files: dict):
    for name, data in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def run_all(d: Path):
    """跑一次 report.py --html，返回 (report.json, report.html, report.md)。"""
    r = subprocess.run([sys.executable, str(SCRIPT), str(d), "--html"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    rep = json.loads((d / "report.json").read_text(encoding="utf-8"))
    return (rep,
            (d / "report.html").read_text(encoding="utf-8"),
            (d / "report.md").read_text(encoding="utf-8"))


def card(html: str, key: str) -> str:
    """取单个指标卡的 HTML 片段（靠 `id='m-N'` 锚点切分）。"""
    i = html.index(f"id='m-{key.lstrip('#')}'")
    j = html.find("id='m-", i + 6)
    return html[i:j if j != -1 else len(html)]


def judge(score=1.0, items=None) -> dict:
    items = items if items is not None else [{"name": "检查点", "pass": True, "quote": "引文"}]
    return {"items": items, "score": score, "evidence": [], "reason": "评审理由"}


def all_pass_inputs() -> dict:
    """16 条指标全 pass 的最小输入集（用于"全过＝无建议"与总览表断言）。"""
    files = {
        "static.json": {"name": "s", "description": "d", "description_tokens": 50,
                        "invoke": {"allowed": ["both", "human"], "resolved": "both", "note": ""},
                        "dangerous": [], "secrets": [], "injection": [], "exfil": [],
                        "obfuscation": [], "pii": [], "hardcoded": [], "stale_refs": [],
                        "excluded": [], "ignored": [], "scan_excluded_dirs": [],
                        "skill_md_body_lines": 70, "skill_md_body_max_lines": 150,
                        "errors": [], "warnings": [], "issues": [], "passed": True},
        "score.json": {
            "trigger": {"precision": 1.0, "recall": 1.0, "f1": 1.0},
            "cost": {k: {"mean": 10, "std": 0, "n": 3, "min": 10, "max": 10}
                     for k in ("tool_calls", "tokens", "seconds")},
            "cost_by_case": {"c1": {"tool_calls": {"mean": 10, "std": 0, "n": 2, "min": 10, "max": 10}},
                             "c2": {"tool_calls": {"mean": 12, "std": 0, "n": 2, "min": 12, "max": 12}}},
            "necessity": {k: {"golden": 8, "baseline": 10, "improvement": 0.2}
                          for k in ("tokens", "tool_calls", "seconds")},
            "passed": True},
        "golden.json": {"steps": [], "answer": "a", "errors": [], "tokens": 10, "seconds": 1},
        "compare.json": {"mean_score": 2.0, "match": True, "note": "2/2 匹配",
                         "per_case": [{"name": "c1", "match": True, "score": 2, "reason": "对"}]},
        "idem.json": {"ratio": 0.1, "idempotent": True, "pairs": 3, "idempotent_pairs": 3,
                      "per_pair": [{"case": "c1", "pair": "a->b", "ratio": 0.1,
                                    "repeated_steps": 1, "total_steps": 10, "idempotent": True}]},
    }
    for name in JUDGES:
        files[f"judges/{name}.json"] = judge()
    return files


def base(tmp_path: Path) -> Path:
    d = tmp_path / "results"
    d.mkdir(exist_ok=True)
    return d


# --- 需求 1：原始 JSON 表格化 + 折叠 ---

def test_every_metric_card_has_a_table(tmp_path):
    """16 条指标全部有表——不再有卡片裸奔原始 JSON（#1/#11/#3 这类 KV 走通用兜底）。"""
    d = base(tmp_path)
    write_inputs(d, all_pass_inputs())
    _rep, html, _md = run_all(d)
    missing = [k for k in ALL_KEYS if "<table" not in card(html, k)]
    assert missing == [], f"这些指标卡没有表格：{missing}"


def test_raw_json_folded_when_table_present(tmp_path):
    """有表就折叠原始 JSON（#1 的 data 很短，旧实现直接内联）。"""
    d = base(tmp_path)
    write_inputs(d, all_pass_inputs())
    _rep, html, _md = run_all(d)
    c2 = card(html, "#1")
    assert "<table" in c2 and "<details" in c2
    assert "name_chars" in c2   # 全文仍在折叠块里（信息不比 json 少）


def test_generic_fallback_flattens_unknown_shape(tmp_path):
    """无定制表的指标（#3）走通用兜底：未知形状递归压平成 key-value 表。"""
    d = base(tmp_path)
    files = all_pass_inputs()
    files["static.json"]["invoke"] = {"resolved": "both", "allowed": ["both", "human"],
                                     "note": "n", "extra": {"deep": {"k": 1}}}
    write_inputs(d, files)
    _rep, html, _md = run_all(d)
    c7 = card(html, "#3")
    assert "<table" in c7
    assert "extra.deep.k" in c7          # 嵌套 dict 的键被压平可见
    assert "<details" in c7              # 有表 → 原始 JSON 折叠


def test_4_card_shows_scan_groups_as_table(tmp_path):
    """#4 六组规则 → 表格（组名 + 命中数），不再是原始 JSON。"""
    d = base(tmp_path)
    files = all_pass_inputs()
    files["static.json"]["secrets"] = [{"pattern": "p", "line": 3, "file": "scripts/x.py",
                                        "text": 'api_key = "sk-live-abcdefghijklmnopqrst"'}]
    files["static.json"]["errors"] = ["发现 1 处硬编码凭据（#4）"]
    write_inputs(d, files)
    _rep, html, _md = run_all(d)
    c13 = card(html, "#4")
    assert "secrets" in c13 and "<table" in c13
    assert "scripts/x.py" in c13          # 命中位置可见


# --- 需求 3：可移植性闸门可见（原本整体 fail 却不出现在页面上）---

def test_portability_gate_fails_4_and_is_visible(tmp_path):
    d = base(tmp_path)
    files = all_pass_inputs()
    files["static.json"]["hardcoded"] = [
        {"pattern": r"[A-Za-z]:[\\/](?:Users|home)[\\/]", "line": 7,
         "file": "SKILL.md", "text": "python C:/Users/someone/.venv/bin/python x.py"}]
    files["static.json"]["errors"] = ["发现 1 处宿主环境硬编码（可移植性闸门，ADR-0008）"]
    files["static.json"]["passed"] = False
    write_inputs(d, files)
    rep, html, _md = run_all(d)
    assert rep["metrics"]["#4"]["verdict"] == "fail"
    assert rep["conclusion"] == "has-failures"        # 闸门整体 fail 必须能影响结论
    c13 = card(html, "#4")
    assert "可移植性闸门" in c13
    assert "SKILL.md" in c13                          # 命中文件可见


def test_stale_refs_and_excluded_dirs_are_informational(tmp_path):
    """stale_refs / scan_excluded_dirs 只作信息行，不改 #4 裁决（ADR-0008 只查硬编码）。"""
    d = base(tmp_path)
    files = all_pass_inputs()
    files["static.json"]["stale_refs"] = ["docs/reference.md"]
    files["static.json"]["scan_excluded_dirs"] = ["tests"]
    write_inputs(d, files)
    rep, html, _md = run_all(d)
    assert rep["metrics"]["#4"]["verdict"] == "pass"
    c13 = card(html, "#4")
    assert "docs/reference.md" in c13 and "tests" in c13


# --- 需求 6：PII（静态组 + 评审折进 #4，取较差者）---

def test_pii_static_hits_warn_not_fail(tmp_path):
    d = base(tmp_path)
    files = all_pass_inputs()
    files["static.json"]["pii"] = [{"pattern": "email", "line": 2, "file": "SKILL.md",
                                    "text": "联系 zhangsan@corp-internal.cn 获取工号 88123"}]
    files["static.json"]["warnings"] = ["发现 1 处个人信息（#4，仅警告）"]
    write_inputs(d, files)
    rep, html, _md = run_all(d)
    assert rep["metrics"]["#4"]["verdict"] == "warn"
    assert "pii" in card(html, "#4")


def test_pii_judge_can_fail_4(tmp_path):
    """语义面（工号/客户名/内部系统地址）判 0 → #4 fail（静态面看不到这些）。"""
    d = base(tmp_path)
    files = all_pass_inputs()
    files["judges/pii.json"] = judge(0.0, [
        {"name": "无公司内部标识", "pass": False, "quote": "工号 88123 / 内部系统 ops.corp.local"},
        {"name": "示例数据全为占位", "pass": False, "quote": "真实客户名"},
        {"name": "无真实个人标识", "pass": True, "quote": "—"},
        {"name": "无未脱敏日志样本", "pass": False, "quote": "logs/2026-09.csv"}])
    write_inputs(d, files)
    rep, html, _md = run_all(d)
    assert rep["metrics"]["#4"]["verdict"] == "fail"
    assert "工号 88123" in card(html, "#4")


def test_pii_judge_partial_is_warn(tmp_path):
    d = base(tmp_path)
    files = all_pass_inputs()
    files["judges/pii.json"] = judge(0.5, [{"name": "无公司内部标识", "pass": False, "quote": "工号 88123"},
                                          {"name": "无真实个人标识", "pass": True, "quote": "—"}])
    write_inputs(d, files)
    rep, _html, _md = run_all(d)
    assert rep["metrics"]["#4"]["verdict"] == "warn"


# --- 需求 5：依赖全面性 → #8 至少 warn ---

def test_deps_missing_downgrades_8_to_warn(tmp_path):
    d = base(tmp_path)
    files = all_pass_inputs()
    files["static.json"]["deps_missing"] = ["requests", "pi"]
    write_inputs(d, files)
    rep, html, _md = run_all(d)
    assert rep["metrics"]["#12"]["verdict"] == "warn"
    assert "requests" in rep["metrics"]["#12"]["note"]
    assert "requests" in card(html, "#12")


def test_deps_missing_does_not_fail_8(tmp_path):
    """缺声明是文档缺陷，不 fail（与 trace 面"跑得通"的证据冲突，ADR-0031）。"""
    d = base(tmp_path)
    files = all_pass_inputs()
    files["static.json"]["deps_missing"] = ["requests"]
    write_inputs(d, files)
    rep, _html, _md = run_all(d)
    assert rep["metrics"]["#12"]["verdict"] != "fail"


# --- 需求 2：优化建议（顶层 advice + 卡片内联 + 汇总 tab + md 段）---

def partial_inputs() -> dict:
    """混合裁决：1 个 fail（#4 闸门）、1 个 warn（#6 fallback 部分过）、1 个 skipped（#12 无 golden）。"""
    files = all_pass_inputs()
    files["static.json"]["hardcoded"] = [{"pattern": "posix", "line": 7, "file": "SKILL.md",
                                          "text": "python /Users/someone/x.py"}]
    files["static.json"]["passed"] = False
    files["judges/fallback.json"] = judge(0.5, [
        {"name": "有降级声明", "pass": False, "quote": "未找到降级声明"},
        {"name": "失败可恢复", "pass": True, "quote": "重试 3 次"}])
    del files["golden.json"]
    return files


def test_advice_covers_every_non_pass_metric(tmp_path):
    d = base(tmp_path)
    write_inputs(d, partial_inputs())
    rep, _html, _md = run_all(d)
    advice = rep["advice"]
    assert advice, "有非 pass 指标时必须产出建议"
    non_pass = {k for k, v in rep["metrics"].items() if v["verdict"] != "pass"}
    covered = {a["metric"] for a in advice}
    assert non_pass <= covered, f"这些指标没有对应建议：{non_pass - covered}"
    assert covered <= set(rep["metrics"]), "建议不得指向不存在的指标"
    for a in advice:
        assert a["severity"] in ("fail", "warn", "info")
        assert a["problem"].strip() and a["action"].strip(), a
        assert isinstance(a["evidence"], str)


def test_advice_severity_follows_verdict(tmp_path):
    d = base(tmp_path)
    write_inputs(d, partial_inputs())
    rep, _html, _md = run_all(d)
    sev = {}
    for k, v in rep["metrics"].items():
        if v["verdict"] != "pass":
            sev[k] = v["verdict"]
    for a in rep["advice"]:
        if a["problem"].startswith("检查点未通过") or a["problem"].startswith("静态规则命中"):
            continue
        assert {"fail": "fail", "warn": "warn", "skipped": "info"}[sev[a["metric"]]] == a["severity"], a


def test_advice_empty_when_all_pass(tmp_path):
    d = base(tmp_path)
    write_inputs(d, all_pass_inputs())
    rep, _html, _md = run_all(d)
    assert rep["conclusion"] == "all-pass"
    assert rep["advice"] == []


def test_advice_inline_under_card_and_in_tab(tmp_path):
    d = base(tmp_path)
    write_inputs(d, partial_inputs())
    rep, html, _md = run_all(d)
    # 卡片内联：命中的指标卡里有 .adv 块
    assert "class='adv'" in card(html, "#4")
    # 汇总 tab + 导航按钮（tab 内是模板属性，用双引号；指标卡用单引号）
    assert 'id="t-advice"' in html and 'data-t="advice"' in html
    tab = html.split('id="t-advice"')[1]
    assert "#4" in tab
    assert "href='#m-4'" in tab          # 锚点跳回指标卡
    # 每条建议都出现在汇总里
    for a in rep["advice"]:
        assert a["problem"] in tab, a["problem"]


def test_advice_skipped_gives_measurement_action(tmp_path):
    """未测不是问题（评审 P1）：skipped 指标的 action 必须是"补测"，不能是"修问题"的方子。"""
    d = base(tmp_path)
    write_inputs(d, partial_inputs())
    rep, html, _md = run_all(d)
    assert rep["metrics"]["#12"]["verdict"] == "skipped"
    top = [a for a in rep["advice"] if a["metric"] == "#12" and a["problem"].startswith("最小依赖")]
    assert top, "未测仍应有条目（未测是缺口，不是丢信息）"
    assert "补测" in top[0]["action"], top[0]
    assert top[0]["action"] != ADVICE_FIX_ACTION_12, "未测指标不得给修问题的动作"
    assert "补测" in card(html, "#12")          # 内联建议同步


def test_advice_includes_nested_judge_checkpoints(tmp_path):
    """#12/#4 的评审 items 嵌在 deps_judge/pii_judge 子键下，未通过的检查点也要出建议条目。"""
    d = base(tmp_path)
    files = all_pass_inputs()
    files["static.json"]["deps_missing"] = ["requests"]
    files["judges/deps.json"] = judge(0.5, [
        {"name": "依赖已声明", "pass": False, "quote": "文档里没有列出 requests"},
        {"name": "凭据走 fallback", "pass": True, "quote": "—"}])
    files["judges/pii.json"] = judge(0.75, [
        {"name": "无公司内部标识", "pass": False, "quote": "工号 88123"},
        {"name": "无真实个人标识", "pass": True, "quote": "—"}])
    write_inputs(d, files)
    rep, _html, _md = run_all(d)
    p8 = [a["problem"] for a in rep["advice"] if a["metric"] == "#12"]
    p13 = [a["problem"] for a in rep["advice"] if a["metric"] == "#4"]
    assert any("检查点未通过：依赖已声明" in p for p in p8), p8
    assert any("检查点未通过：无公司内部标识" in p for p in p13), p13
    assert any("工号 88123" == a["evidence"] for a in rep["advice"]), "证据要带上原文引用"


def test_advice_section_in_md(tmp_path):
    d = base(tmp_path)
    write_inputs(d, partial_inputs())
    _rep, _html, md = run_all(d)
    assert "## 优化建议" in md
    assert "#4" in md.split("## 优化建议")[1]


# --- 需求 4：结果栏 ✓/✗ ---

def test_verdict_glyphs_and_overview_table(tmp_path):
    d = base(tmp_path)
    write_inputs(d, partial_inputs())
    rep, html, _md = run_all(d)
    assert "id='overview'" in html
    for k in ALL_KEYS:
        assert f"<td>{k}</td>" in html, f"总览表缺 {k}"
    # 四档符号：passed 卡用绿 ✓，fail 卡用红 ✗
    assert "vf-fail" in card(html, "#4") and "✗" in card(html, "#4")
    assert "vf-pass" in card(html, "#10") and "✓" in card(html, "#10")
    assert "vf-warn" in card(html, "#6") and "⚠" in card(html, "#6")
    assert "vf-skipped" in card(html, "#12") and "–" in card(html, "#12")
    assert rep["metrics"]["#4"]["verdict"] == "fail"
