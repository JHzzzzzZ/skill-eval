"""Seam: python scripts/report.py <results目录> [--out <前缀>] -> stdout JSON 摘要 + 落盘 report.json/report.md

输入（同目录，均可缺省→对应指标 skipped）：
- static.json（static_check.py stdout）
- score.json（score.py stdout）
- idem.json（{"ratio", "idempotent"}，多次 idem.py 的聚合）
- compare.json（compare.py 输出，#9）
- golden.json（golden trace，#8）
- judges/<metric>.json（judge_runner 校验过的输出）：brevity(#3) redundancy(#6) fallback(#11)
  precheck(#16) contract(#17,#18) side-effects(#19)

report.json 契约（ADR-0004）：19 个 key 全出现；值 = {"verdict": pass|warn|fail|skipped,
"method": 静态/运行/LLM评审/对比, "data": {...}, "note"}；不合成总分；
conclusion 三档：all-pass / with-warnings / has-failures（全 skipped 也算 with-warnings）。
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "report.py"

ALL_KEYS = ["#1", "#2", "#3", "#4", "#5", "#6", "#7", "#8", "#9", "#10",
            "#11", "#12", "#13", "#14", "#15", "#16", "#17", "#18", "#19"]


def write_inputs(d: Path, files: dict):
    for name, data in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def run_report(d: Path):
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(d)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"report.py failed: {r.stderr}"
    return json.loads((d / "report.json").read_text(encoding="utf-8"))


# --- Slice 15: 19 key 恒定 + skipped 语义 ---

def test_empty_inputs_all_skipped(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    out = run_report(tmp_path)
    assert len(out["metrics"]) == 19
    for k, v in out["metrics"].items():
        assert v["verdict"] == "skipped", f"{k} 应 skipped"
    assert out["conclusion"] == "with-warnings"  # 全 skipped 不是 all-pass


# --- Slice 16: 静态检查映射（#2/#7/#13）---

def test_static_inputs(tmp_path):
    write_inputs(tmp_path, {"static.json": {
        "name": "s", "description": "d", "description_tokens": 50,
        "invoke": {"resolved": "both"},
        "errors": [], "warnings": [], "dangerous": [], "passed": True}})
    out = run_report(tmp_path)
    m = out["metrics"]
    assert m["#2"]["verdict"] == "pass"
    assert m["#7"]["verdict"] == "pass"
    assert m["#13"]["verdict"] == "pass"
    assert out["conclusion"] == "with-warnings"  # 其余仍 skipped


def test_static_dangerous_is_fail(tmp_path):
    write_inputs(tmp_path, {"static.json": {
        "name": "s", "description": "d", "description_tokens": 50,
        "invoke": {"resolved": "both"},
        "errors": ["发现 1 处危险命令模式"], "warnings": [],
        "dangerous": [{"pattern": "x"}], "passed": False}})
    out = run_report(tmp_path)
    assert out["metrics"]["#13"]["verdict"] == "fail"
    assert out["conclusion"] == "has-failures"


# --- Slice 17: 触发/成本/必要性/幂等/对比 ---

def test_score_inputs(tmp_path):
    write_inputs(tmp_path, {"score.json": {
        "trigger": {"precision": 1.0, "recall": 1.0, "f1": 1.0},
        "cost": {"tool_calls": {"mean": 3, "std": 1}, "tokens": {"mean": 100, "std": 0},
                 "seconds": {"mean": 10, "std": 0}},
        "necessity": {"tokens": {"golden": 100, "baseline": 300, "improvement": 0.66},
                      "tool_calls": "skipped", "seconds": "skipped"},
        "passed": True}})
    out = run_report(tmp_path)
    m = out["metrics"]
    assert m["#1"]["verdict"] == "pass"   # f1 >= 0.7
    assert m["#4"]["verdict"] == "pass"   # 有成本数据即 info-pass
    assert m["#5"]["verdict"] == "pass"   # improvement > 0


def test_low_f1_is_fail(tmp_path):
    write_inputs(tmp_path, {"score.json": {
        "trigger": {"precision": 0.5, "recall": 0.5, "f1": 0.5},
        "cost": "skipped", "necessity": "skipped", "passed": True}})
    out = run_report(tmp_path)
    assert out["metrics"]["#1"]["verdict"] == "fail"


def test_idem_and_compare(tmp_path):
    write_inputs(tmp_path, {
        "idem.json": {"ratio": 0.2, "idempotent": True},
        "compare.json": {"match": True, "score": 1, "reason": "ok"}})
    out = run_report(tmp_path)
    assert out["metrics"]["#15"]["verdict"] == "pass"
    assert out["metrics"]["#9"]["verdict"] == "pass"


# --- Slice 18: LLM 评审映射 ---

def test_judge_inputs(tmp_path):
    write_inputs(tmp_path, {"judges/redundancy.json": {"score": 1.0, "evidence": [], "reason": "x"},
                            "judges/fallback.json": {"score": 0.0, "evidence": ["a"], "reason": "无 fallback"}})
    out = run_report(tmp_path)
    m = out["metrics"]
    assert m["#6"]["verdict"] == "pass"   # ratio 1.0
    assert m["#11"]["verdict"] == "fail"  # ratio 0.0


# --- Slice 19: report.md 产出 ---

def test_report_md_written(tmp_path):
    write_inputs(tmp_path, {"static.json": {
        "name": "s", "description": "d", "description_tokens": 50,
        "invoke": {"resolved": "both"}, "errors": [], "warnings": [],
        "dangerous": [], "passed": True}})
    run_report(tmp_path)
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "#1" in md and "结论" in md and "pass" in md


# --- 全仓复审 P2: #12 稳定性阈值（变异系数）---

def test_idempotency_high_variance_warns(tmp_path):
    write_inputs(tmp_path, {"score.json": {
        "trigger": "skipped",
        "cost": {"tool_calls": {"mean": 10, "std": 6, "n": 3}, "tokens": {"mean": 100, "std": 20, "n": 3},
                 "seconds": {"mean": 10, "std": 0, "n": 3}},
        "necessity": "skipped", "passed": True}})
    out = run_report(tmp_path)
    # 变异系数 6/10 = 0.6 > 0.5 → 不稳定
    assert out["metrics"]["#12"]["verdict"] == "warn"
    # 回归：note 必须与 verdict 一致（原实现 n>=2 时恒写"≤ 阈值，稳定"）
    assert "不稳定" in out["metrics"]["#12"]["note"]
    assert "≤" not in out["metrics"]["#12"]["note"]


def test_stability_note_matches_verdict_when_stable(tmp_path):
    # cv ≤ 阈值且 n≥2：pass + note 写"≤ 阈值，稳定"
    write_inputs(tmp_path, {"score.json": {
        "trigger": "skipped",
        "cost": {"tool_calls": {"mean": 10, "std": 2, "n": 3}, "tokens": "skipped",
                 "seconds": "skipped"},
        "necessity": "skipped", "passed": True}})
    out = run_report(tmp_path)
    assert out["metrics"]["#12"]["verdict"] == "pass"
    assert "≤" in out["metrics"]["#12"]["note"]
    assert "稳定" in out["metrics"]["#12"]["note"]


# --- #8 双源裁决（#29/#30）：trace 报错 + judges/deps.json ---

def test_deps_judge_semantic_fail(tmp_path):
    # golden 无报错，但语义评审发现未打包依赖 → fail
    write_inputs(tmp_path, {"golden.json": {"steps": [], "errors": []},
                            "judges/deps.json": {"items": [{"name": "无未打包依赖", "pass": False, "quote": "q"}],
                                                 "score": 0.0, "evidence": ["e"], "reason": "缺"}})
    out = run_report(tmp_path)
    assert out["metrics"]["#8"]["verdict"] == "fail"


def test_deps_judge_partial_warn(tmp_path):
    write_inputs(tmp_path, {"golden.json": {"steps": [], "errors": []},
                            "judges/deps.json": {"items": [{"name": "a", "pass": True, "quote": "q"},
                                                           {"name": "b", "pass": False, "quote": "q"}],
                                                 "score": 0.5, "evidence": ["e"], "reason": "部分"}})
    out = run_report(tmp_path)
    assert out["metrics"]["#8"]["verdict"] == "warn"


def test_deps_trace_error_overrides_semantic_pass(tmp_path):
    # 实跑报错优先级高于语义面全过 → 仍 fail
    write_inputs(tmp_path, {"golden.json": {"steps": [], "errors": ["ModuleNotFoundError: x"]},
                            "judges/deps.json": {"items": [{"name": "a", "pass": True, "quote": "q"}],
                                                 "score": 1.0, "evidence": [], "reason": "全过"}})
    out = run_report(tmp_path)
    assert out["metrics"]["#8"]["verdict"] == "fail"


# --- #19/#20：report.md 按指标 ID 排序 + 每处标注中文含义 ---

def test_report_md_ordered_with_chinese_labels(tmp_path):
    write_inputs(tmp_path, {"score.json": {"trigger": {"precision": 1.0, "recall": 1.0, "f1": 1.0},
                                           "cost": "skipped", "necessity": "skipped", "passed": True},
                            "static.json": {"name": "s", "description": "d", "description_tokens": 5,
                                            "invoke": {"resolved": "both"}, "dangerous": [],
                                            "errors": [], "warnings": [], "passed": True}})
    out = run_report(tmp_path)
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    # 19 个指标全部出现，按 ID 顺序
    ids = [f"#{i}" for i in range(1, 20)]
    pos = [md.index(f"### {k} · ") for k in ids]
    assert pos == sorted(pos)
    # 每处标注中文含义（不能只写 #N）
    assert "#1 · 触发精准度" in md
    assert "#17 · 输入契约" in md
    assert "#18 · 输出契约" in md
    # method 用中文标注
    assert "（沙箱运行）" in md and "（静态检查）" in md


# --- #24/#38/#43：--html 静态报告页（双 tab + SVG 流程） ---

def test_html_report_written(tmp_path):
    write_inputs(tmp_path, {"score.json": {"trigger": {"precision": 1.0, "recall": 1.0, "f1": 1.0},
                                           "cost": "skipped", "necessity": "skipped", "passed": True}})
    r = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path), "--html"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert (tmp_path / "report.json").is_file()  # 原有产物不丢
    assert "触发精准度" in html            # 中文指标名（#20）
    assert "<svg" in html                 # 流程图（#43）
    assert "评测集" in html               # 审核页 tab（#38）
    assert html.count("<script") <= 1 and "http" not in html.split("<body")[1][:2000]  # 单文件、无外链


def test_html_report_with_evalset(tmp_path):
    write_inputs(tmp_path, {})
    ev = tmp_path / "evalset"
    (ev / "triggers" / "should").mkdir(parents=True)
    (ev / "triggers" / "should" / "s1.json").write_text(json.dumps({"prompt": "帮我记个待办"}), encoding="utf-8")
    (ev / "cases").mkdir()
    (ev / "cases" / "k1.json").write_text(json.dumps({"prompt": "记 todo", "expect": "登记成功"}), encoding="utf-8")
    r = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path), "--html", "--evalset", str(ev)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "帮我记个待办" in html
    assert "登记成功" in html


# --- 坏产物兜底（#41 类）：编码错误的中间产物 → 对应指标 skipped，整体不崩 ---

def test_gbk_artifact_no_crash(tmp_path):
    d = tmp_path
    (d / "static.json").write_bytes('{"name": "s", "description": "需求受理", "description_tokens": 5, "invoke": {}, "dangerous": [], "errors": [], "warnings": [], "passed": true}'.encode("gbk"))
    r = subprocess.run([sys.executable, str(SCRIPT), str(d)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads((d / "report.json").read_text(encoding="utf-8"))
    # gbk 兼容读取成功：#2 有数据，不是 skipped
    assert out["metrics"]["#2"]["data"] is not None


def test_html_tab_script_balanced(tmp_path):
    # 回归：tab 切换脚本花括号必须配对（曾因模板转义残留多一个 } 导致点击失效）
    write_inputs(tmp_path, {})
    r = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path), "--html"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    script = html.split("<script>")[1].split("</script>")[0]
    assert script.count("{") == script.count("}")
    assert "t-flow" in html and "t-evalset" in html


def test_evalset_review_ui(tmp_path):
    # 审核交互：每条可标 通过/驳回 + 备注 + 导出 review.json
    write_inputs(tmp_path, {})
    ev = tmp_path / "evalsets" / "todo-add" / "v1"
    (ev / "triggers" / "should").mkdir(parents=True)
    (ev / "triggers" / "should" / "s1.json").write_text(json.dumps({"prompt": "帮我记个待办"}), encoding="utf-8")
    (ev / "cases").mkdir()
    (ev / "cases" / "k1.json").write_text(json.dumps({"prompt": "记 todo", "expect": "登记成功"}), encoding="utf-8")
    r = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path), "--html", "--evalset", str(ev)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    # 每条用例带审核控件与定位信息
    assert "class='card rv-item'" in html
    assert "data-group='应触发'" in html and "data-file='s1.json'" in html
    assert "通过" in html and "驳回" in html
    # 审核标准说明（教人怎么审）
    assert "审核标准" in html
    # 导出：复制 + 下载，携带评测集名与版本
    assert "review.json" in html and "todo-add" in html and "v1" in html
    assert "navigator.clipboard" in html  # 复制到剪贴板
    # 驳回必须填原因的前端提示
    assert "驳回原因" in html


def test_evalset_review_shows_skill_meta(tmp_path):
    # 审核人得先知道在审什么：顶部展示被测 skill 的 name/description（--skill 指定）
    write_inputs(tmp_path, {})
    ev = tmp_path / "evalsets" / "todo-add" / "v1"
    (ev / "triggers" / "should").mkdir(parents=True)
    (ev / "triggers" / "should" / "s1.json").write_text(json.dumps({"prompt": "帮我记个待办"}), encoding="utf-8")
    sk = tmp_path / "myskill"
    sk.mkdir()
    (sk / "SKILL.md").write_text("---\nname: todo-add\ndescription: 需求受理登记，用户说加个需求时触发\n---\n# t\n", encoding="utf-8")
    r = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path), "--html", "--evalset", str(ev),
                        "--skill", str(sk)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "被测 Skill" in html and "需求受理登记" in html
    assert "id='rv-skill'" in html


def test_evalset_skill_meta_uploads_fallback(tmp_path):
    # 不传 --skill 时，兖底读评估器 uploads/<name>-<时间戳>/SKILL.md 存档（取最新）
    write_inputs(tmp_path, {})
    ev = tmp_path / "evalsets" / "todo-add" / "v1"
    (ev / "triggers" / "should").mkdir(parents=True)
    (ev / "triggers" / "should" / "s1.json").write_text(json.dumps({"prompt": "帮我记个待办"}), encoding="utf-8")
    up = tmp_path / "uploads" / "todo-add-20260920-115935"
    up.mkdir(parents=True)
    (up / "SKILL.md").write_text("---\nname: todo-add\ndescription: 兖底存档描述\n---\n# t\n", encoding="utf-8")
    r = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path), "--html", "--evalset", str(ev)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "兖底存档描述" in html


def test_evalset_review_submit_button(tmp_path):
    # 审核完成后一键提交：showSaveFilePicker 直接落盘 review.json，不再只靠复制/下载
    write_inputs(tmp_path, {})
    ev = tmp_path / "evalsets" / "todo-add" / "v1"
    (ev / "triggers" / "should").mkdir(parents=True)
    (ev / "triggers" / "should" / "s1.json").write_text(json.dumps({"prompt": "帮我记个待办"}), encoding="utf-8")
    r = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path), "--html", "--evalset", str(ev)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "rvSubmit" in html and "showSaveFilePicker" in html
    assert "提交 review.json" in html
    # 脚本花括号仍配对（回归护栏）
    script = html.split("<script>")[1].split("</script>")[0]
    assert script.count("{") == script.count("}")
