"""report.py 的元数据与可信度折入（ADR-0010 / ADR-0011）：

- results/meta.json 落盘：schema + 评估器指纹 + 被测 skill 指纹 + 评测集来源
- report.json 带 evaluator 指纹；report.md 顶部显示指纹
- evalset.json clean=false → #1 降级 warn（fail 保持 fail；#1 skipped 时不动）
- model_robust.json → 折进 #12：组内成本与跨模型一致率分写；跨模型**只降级不升级**
- data 渲染层（本文件后半）：表格优先 + 原始 JSON 折叠保留 + 不留静默截断（>400 字符只留指针）
"""
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "scripts"
SCRIPT = SCRIPTS / "report.py"


def write_inputs(d: Path, files: dict):
    for name, data in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def run_report(d: Path, *extra):
    """跑 report.py，返回落盘的 report.json（stdout 摘要另看 run_report_stdout）。"""
    run_report_stdout(d, *extra)
    return load(d, "report.json")


def run_report_stdout(d: Path, *extra):
    r = subprocess.run([sys.executable, str(SCRIPT), str(d), *extra],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def load(d: Path, name: str):
    return json.loads((d / name).read_text(encoding="utf-8"))


def stable_cost(n=3):
    return {"trigger": "skipped",
            "cost": {k: {"mean": 3, "std": 0, "n": n, "min": 3, "max": 3}
                     for k in ("tool_calls", "tokens", "seconds")},
            "necessity": "skipped", "passed": True}


# --- data 渲染层（表格优先 / 折叠原始 JSON / 不留静默截断）---

def long_idem(pairs: int = 15) -> dict:
    """15 对相邻跑（run3 实测形状），末尾带标记串用于检测头截断。"""
    per = [{"case": f"c{(i % 5) + 1}", "pair": f"golden-c{(i % 5) + 1}->repeat-c{(i % 5) + 1}-{(i % 3) + 1}",
            "ratio": round(0.09 * (i + 1), 4), "repeated_steps": i, "total_steps": 11 + i,
            "idempotent": True} for i in range(pairs - 1)]
    per.append({"case": "c5", "pair": "TAIL-MARKER->repeat-c5-3", "ratio": 0.0,
                "repeated_steps": 0, "total_steps": 11, "idempotent": True})
    return {"ratio": 0.1478, "idempotent": True, "pairs": pairs, "idempotent_pairs": pairs, "per_pair": per}


def run_html_md(d: Path):
    r = subprocess.run([sys.executable, str(SCRIPT), str(d), "--html"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    return ((d / "report.html").read_text(encoding="utf-8"),
            (d / "report.md").read_text(encoding="utf-8"))


def base_inputs(idem=None, compare=None, brevity=1) -> dict:
    cost = {"tool_calls": {"mean": 10.8, "std": 6.99, "n": 20, "min": 3, "max": 33},
            "tokens": {"mean": 42195.1, "std": 12498.9, "n": 20, "min": 29848, "max": 83385},
            "seconds": {"mean": 96.3, "std": 67.5, "n": 20, "min": 36.4, "max": 315.6}}
    files = {"static.json": {"name": "s", "description": "d", "description_tokens": 226,
                             "invoke": {"resolved": "both"}, "errors": [], "warnings": [],
                             "dangerous": [], "passed": True},
             "score.json": {"trigger": {"precision": 0.53, "recall": 1.0, "f1": 0.69}, "cost": cost,
                            "cost_by_case": {"c1": {"tool_calls": {"mean": 15.0, "std": 6.82, "n": 4}},
                                             "c2": {"tool_calls": {"mean": 14.2, "std": 10.89, "n": 4}}},
                            "necessity": "skipped", "passed": True}}
    files["judges/brevity.json"] = {"items": [{"name": f"检查点{i}", "pass": True, "quote": f"引文{i}"}
                                               for i in range(brevity)],
                                    "score": 1.0, "evidence": [],
                                    "reason": "正文 70 行，无可下沉细节（judge reason 应出现在 HTML 摘要行）"}
    if idem is not None:
        files["idem.json"] = idem
    if compare is True:
        files["compare.json"] = {"mean_score": 2.0, "match": True, "note": "5/5 case 匹配",
                                 "per_case": [{"name": f"c{i}", "match": True, "score": 2,
                                               "reason": f"case {i} 的理由" * 1} for i in range(1, 6)]}
    return files


# --- 评估器指纹（ADR-0010）---

def test_report_json_carries_evaluator_fingerprint(tmp_path):
    summary = run_report_stdout(tmp_path)
    ev = load(tmp_path, "report.json")["evaluator"]
    assert ev["version"].startswith("auto-")
    assert len(ev["content_sha256"]) == 64
    assert ev["files"] >= 10  # scripts/*.py + judges/*.md + SKILL.md + reference.md
    assert summary["evaluator"] == ev["version"]
    assert summary["meta_schema"] == "skill-eval/results-meta/1"


def test_fingerprint_deterministic_and_content_sensitive(tmp_path):
    sys.path.insert(0, str(SCRIPTS))  # report.py 用同目录导入（_console），importlib 加载需先补 path
    spec = importlib.util.spec_from_file_location("report_mod", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    root = SCRIPTS.parent
    # 指纹只覆盖 scripts/*.py + judges/*.md + SKILL.md + reference.md；
    # .sandbox/.skillrepos/uploads 是沙箱副本与存档（3.2 万文件、node_modules 超 Windows 路径上限），
    # 复制它们既无意义也会让 copytree 报 811 个 WinError 3。
    ignore = shutil.ignore_patterns("__pycache__", "tests", "evalsets", ".pytest_cache",
                                    ".sandbox", ".skillrepos", "uploads", "node_modules", ".git")
    a = mod.evaluator_fingerprint(root)
    assert a == mod.evaluator_fingerprint(root)  # 同内容同指纹（可复现）

    same = tmp_path / "ev-same"
    shutil.copytree(root, same, ignore=ignore)
    assert mod.evaluator_fingerprint(same) == a  # 与绝对路径无关

    changed = tmp_path / "ev-changed"
    shutil.copytree(root, changed, ignore=ignore)
    p = changed / "judges" / "fallback.md"
    p.write_text(p.read_text(encoding="utf-8") + "\n补充一行\n", encoding="utf-8")
    assert mod.evaluator_fingerprint(changed)["content_sha256"] != a["content_sha256"]


def test_missing_root_is_unknown_not_crash(tmp_path):
    sys.path.insert(0, str(SCRIPTS))  # 同上：同目录导入 _console
    spec = importlib.util.spec_from_file_location("report_mod2", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = mod.evaluator_fingerprint(tmp_path / "nope")
    assert out["version"] == "unknown" and out["files"] == 0


def test_report_md_shows_fingerprint(tmp_path):
    run_report(tmp_path)
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "评估器指纹：auto-" in md


# --- results/meta.json（ADR-0010）---

def test_meta_json_written_with_skill_and_evalset(tmp_path):
    skill = tmp_path / "myskill"
    (skill / "sub").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n正文\n", encoding="utf-8")
    (skill / "sub" / "helper.py").write_text("x = 1\n", encoding="utf-8")
    evalset = tmp_path / "evalsets" / "myskill" / "v1"
    evalset.mkdir(parents=True)
    (evalset / "meta.json").write_text(
        json.dumps({"frozen_at": "2026-09-22", "reviewed_by": "human"}), encoding="utf-8")

    run_report(tmp_path, "--skill", str(skill), "--evalset", str(evalset))
    meta = load(tmp_path, "meta.json")
    assert meta["schema"] == "skill-eval/results-meta/1"
    assert meta["evaluator"]["version"].startswith("auto-")
    assert len(meta["skill_fingerprint"]["content_sha256"]) == 64
    assert meta["skill_fingerprint"]["files"] == 2
    assert meta["skill_fingerprint"]["path"] == str(skill)
    assert meta["evalset_source"] == {"name": "myskill", "version": "v1", "frozen": True,
                                      "frozen_at": "2026-09-22", "reviewed_by": "human"}
    assert meta["metrics"] == {"total": 16, "measured": 0, "skipped": 16}
    assert meta["conclusion"] == load(tmp_path, "report.json")["conclusion"]


def test_hand_written_meta_fields_survive(tmp_path):
    # 真实场景：results/meta.json 已有人工运行记录（model/sandbox/skipped_reason），
    # 评估器只能加自己的键，不能把它们冲掉（ADR-0010）
    human = {"skill": "code-review", "skill_path": "/somewhere/code-review", "version": "74ca5fe",
             "evaluated_at": "2026-09-17", "model": "zai/glm-5.3-flash",
             "sandbox": "git worktree: wt-code-review", "evalset": "evalsets/code-review/v1 (draft)",
             "skipped_reason": "#1/#5/#9 评测集未冻结"}
    (tmp_path / "meta.json").write_text(json.dumps(human, ensure_ascii=False), encoding="utf-8")
    run_report(tmp_path)
    meta = load(tmp_path, "meta.json")
    for k, v in human.items():
        assert meta[k] == v, f"人工字段 {k} 被覆盖"
    assert meta["schema"] == "skill-eval/results-meta/1"
    assert meta["evaluator"]["version"].startswith("auto-")


def test_meta_json_without_skill_is_null(tmp_path):
    run_report(tmp_path)
    meta = load(tmp_path, "meta.json")
    assert meta["skill_fingerprint"] is None and meta["evalset_source"] is None


def test_unfrozen_evalset_marked_not_frozen(tmp_path):
    evalset = tmp_path / "evalsets" / "s" / "v2"
    evalset.mkdir(parents=True)
    run_report(tmp_path, "--evalset", str(evalset))
    meta = load(tmp_path, "meta.json")
    assert meta["evalset_source"]["frozen"] is False
    assert "未冻结" in meta["evalset_source"]["note"]


def test_skill_fingerprint_changes_with_content(tmp_path):
    skill = tmp_path / "s1"
    skill.mkdir()
    (skill / "SKILL.md").write_text("v1", encoding="utf-8")
    run_report(tmp_path, "--skill", str(skill))
    a = load(tmp_path, "meta.json")["skill_fingerprint"]["content_sha256"]
    (skill / "SKILL.md").write_text("v2", encoding="utf-8")
    run_report(tmp_path, "--skill", str(skill))
    b = load(tmp_path, "meta.json")["skill_fingerprint"]["content_sha256"]
    assert a != b


# --- #1 触发集质量折入 ---

def test_evalset_quality_downgrades_pass_to_warn(tmp_path):
    write_inputs(tmp_path, {"score.json": {**stable_cost(), "trigger": {"precision": 1.0, "recall": 1.0, "f1": 0.95}},
                            "evalset.json": {"checked": 5, "clean": False, "note": "2 条 should prompt 与 description 高度重合"}})
    out = run_report(tmp_path)
    m1 = out["metrics"]["#10"]
    assert m1["verdict"] == "warn"
    assert m1["data"]["f1"] == 0.95  # 数字仍保留，别丢证据
    assert m1["data"]["evalset_quality"]["clean"] is False
    assert "触发集质量警告" in m1["note"]


def test_evalset_quality_keeps_fail(tmp_path):
    write_inputs(tmp_path, {"score.json": {**stable_cost(), "trigger": {"precision": 0.3, "recall": 0.4, "f1": 0.34}},
                            "evalset.json": {"checked": 5, "clean": False, "note": "照抄"}})
    out = run_report(tmp_path)
    assert out["metrics"]["#10"]["verdict"] == "fail"  # 不因降级把 fail 洗成 warn


def test_evalset_quality_ignored_when_trigger_skipped(tmp_path):
    write_inputs(tmp_path, {"evalset.json": {"checked": 5, "clean": False, "note": "照抄"}})
    out = run_report(tmp_path)
    assert out["metrics"]["#10"]["verdict"] == "skipped"
    assert out["metrics"]["#10"]["data"] is None


def test_clean_evalset_does_not_touch_1(tmp_path):
    write_inputs(tmp_path, {"score.json": {**stable_cost(), "trigger": {"precision": 1.0, "recall": 1.0, "f1": 0.95}},
                            "evalset.json": {"checked": 5, "clean": True, "note": "ok"}})
    out = run_report(tmp_path)
    assert out["metrics"]["#10"]["verdict"] == "pass"
    assert out["metrics"]["#10"]["note"] == "F1 及格线 0.7"


# --- #12 跨模型一致性折入（ADR-0011）---

def test_model_robust_downgrades_stable_cost(tmp_path):
    write_inputs(tmp_path, {"score.json": stable_cost(),
                            "model_robust.json": {"skipped": False, "robust": False, "models": ["a", "b"],
                                                  "agreement_ratio": 0.25, "note": "2 个模型 × 4 个 case：一致 1/4"}})
    out = run_report(tmp_path)
    m12 = out["metrics"]["#15"]
    assert m12["verdict"] == "warn"
    assert m12["data"]["intra_model_cost"]["cost"]["tool_calls"]["n"] == 3  # 两信号分写
    assert m12["data"]["model_robust"]["agreement_ratio"] == 0.25
    assert "跨模型一致性" in m12["note"]


def test_model_robust_does_not_upgrade_single_run(tmp_path):
    write_inputs(tmp_path, {"score.json": stable_cost(n=1),
                            "model_robust.json": {"skipped": False, "robust": True, "models": ["a", "b"],
                                                  "agreement_ratio": 1.0, "note": "一致"}})
    out = run_report(tmp_path)
    assert out["metrics"]["#15"]["verdict"] == "warn"  # 单次运行不能靠跨模型洗成 pass


def test_model_robust_gives_12_evidence_without_cost_data(tmp_path):
    write_inputs(tmp_path, {"model_robust.json": {"skipped": False, "robust": True, "models": ["a", "b"],
                                                  "agreement_ratio": 1.0, "note": "一致"}})
    out = run_report(tmp_path)
    assert out["metrics"]["#15"]["verdict"] == "pass"
    assert out["metrics"]["#15"]["data"]["intra_model_cost"] is None


def test_model_robust_skipped_is_not_folded(tmp_path):
    write_inputs(tmp_path, {"score.json": stable_cost(),
                            "model_robust.json": {"skipped": True, "robust": None,
                                                  "note": "只有 1 个模型的 trace"}})
    out = run_report(tmp_path)
    assert out["metrics"]["#15"]["verdict"] == "pass"  # 与未启用跨模型时一致
    assert out["metrics"]["#15"]["data"]["cost"]["tool_calls"]["n"] == 3


# --- data 渲染层回归（本地批次）---

def test_long_data_not_truncated_in_html(tmp_path):
    # 回归：旧实现 [:2000] 会把 2165 字符的 #16 data 切成非法 JSON（尾部的 TAIL-MARKER 丢失）
    write_inputs(tmp_path, base_inputs(idem=long_idem()))
    html, _md = run_html_md(tmp_path)
    assert "TAIL-MARKER" in html                      # 尾部内容活着（没被 2000 截断）
    assert "<details" in html and "per_pair" in html  # 原始 data 折叠保留（report.json 同源，HTML 里引号被转义）
    assert html.count("<table") >= 1
    raw = json.dumps(long_idem(), ensure_ascii=False, indent=2)
    assert len(raw) > 2000                             # 前提：这条 data 确实超过旧阈值


def test_md_every_data_rendered_as_table(tmp_path):
    # ADR-0030 改写了本用例：**旧契约**是"无表可渲染的长 data 只留 report.json 指针"，
    # 新契约要求所有指标 data 一律表格化（需求 1）——指针分支因此不再可达。
    write_inputs(tmp_path, base_inputs(idem=long_idem()))
    html, md = run_html_md(tmp_path)
    # #16：Markdown 表格 + 摘要行，不再重复整块 JSON
    idem_sec = md.split("### #16")[1].split("### ")[0]
    assert "| case | pair | ratio |" in idem_sec and "ratio 0.148" in idem_sec  # _fmt 保留 3 位
    # 短 data（#1 只有 description_tokens）同样是表
    assert "| 字段 | 值 |" in md.split("### #1")[1].split("### ")[0]
    assert "```json" not in md                      # 全文本已无裸 JSON
    # HTML 侧：有表就折叠原始 JSON
    assert "<details" in html and "<table" in html


def test_judge_metrics_show_summary_and_raw(tmp_path):
    # 旧 HTML 只渲染 items，丢了 score/reason/evidence；#6 口径要求 html 不得比 md 少信息
    write_inputs(tmp_path, base_inputs(brevity=3))
    html, md = run_html_md(tmp_path)
    assert "正文 70 行，无可下沉细节" in html          # 摘要行含 judge reason
    assert "检查点" in html and "<table" in html
    assert "evidence" in html                          # 原始 judge data 可见（内联或折叠）
    assert "| 检查点 | 结果 | 原文引用 |" in md


def test_long_cell_truncated_but_full_text_in_folded_json(tmp_path):
    long_reason = "理由" + "细节" * 400 + "尾部标记TAIL"
    write_inputs(tmp_path, {"compare.json": {"mean_score": 1.0, "match": True, "note": "1/1 匹配",
                                             "per_case": [{"name": "c1", "match": True, "score": 1,
                                                           "reason": long_reason}]}})
    html, _md = run_html_md(tmp_path)
    assert "…（截断，全文见原始 data）" in html        # 单元格截断并标注
    assert "尾部标记TAIL" in html                      # 全文仍在折叠的原始 data 里
    assert len(long_reason) > 800                     # 前提：确实超过单元格阈值
