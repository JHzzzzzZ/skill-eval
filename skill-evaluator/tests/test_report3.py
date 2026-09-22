"""report.py 的元数据与可信度折入（ADR-0010 / ADR-0011）：

- results/meta.json 落盘：schema + 评估器指纹 + 被测 skill 指纹 + 评测集来源
- report.json 带 evaluator 指纹；report.md 顶部显示指纹
- evalset.json clean=false → #1 降级 warn（fail 保持 fail；#1 skipped 时不动）
- model_robust.json → 折进 #12：组内成本与跨模型一致率分写；跨模型**只降级不升级**
"""
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "scripts"
SCRIPT = SCRIPTS / "report.py"
EVO = SCRIPTS / "evolution.py"


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
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def load(d: Path, name: str):
    return json.loads((d / name).read_text(encoding="utf-8"))


def stable_cost(n=3):
    return {"trigger": "skipped",
            "cost": {k: {"mean": 3, "std": 0, "n": n, "min": 3, "max": 3}
                     for k in ("tool_calls", "tokens", "seconds")},
            "necessity": "skipped", "passed": True}


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
    spec = importlib.util.spec_from_file_location("report_mod", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    root = SCRIPTS.parent
    ignore = shutil.ignore_patterns("__pycache__", "tests", "evalsets", ".pytest_cache")
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
    assert meta["metrics"] == {"total": 19, "measured": 0, "skipped": 19}
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
    m1 = out["metrics"]["#1"]
    assert m1["verdict"] == "warn"
    assert m1["data"]["f1"] == 0.95  # 数字仍保留，别丢证据
    assert m1["data"]["evalset_quality"]["clean"] is False
    assert "触发集质量警告" in m1["note"]


def test_evalset_quality_keeps_fail(tmp_path):
    write_inputs(tmp_path, {"score.json": {**stable_cost(), "trigger": {"precision": 0.3, "recall": 0.4, "f1": 0.34}},
                            "evalset.json": {"checked": 5, "clean": False, "note": "照抄"}})
    out = run_report(tmp_path)
    assert out["metrics"]["#1"]["verdict"] == "fail"  # 不因降级把 fail 洗成 warn


def test_evalset_quality_ignored_when_trigger_skipped(tmp_path):
    write_inputs(tmp_path, {"evalset.json": {"checked": 5, "clean": False, "note": "照抄"}})
    out = run_report(tmp_path)
    assert out["metrics"]["#1"]["verdict"] == "skipped"
    assert out["metrics"]["#1"]["data"] is None


def test_clean_evalset_does_not_touch_1(tmp_path):
    write_inputs(tmp_path, {"score.json": {**stable_cost(), "trigger": {"precision": 1.0, "recall": 1.0, "f1": 0.95}},
                            "evalset.json": {"checked": 5, "clean": True, "note": "ok"}})
    out = run_report(tmp_path)
    assert out["metrics"]["#1"]["verdict"] == "pass"
    assert out["metrics"]["#1"]["note"] == "F1 及格线 0.7"


# --- #12 跨模型一致性折入（ADR-0011）---

def test_model_robust_downgrades_stable_cost(tmp_path):
    write_inputs(tmp_path, {"score.json": stable_cost(),
                            "model_robust.json": {"skipped": False, "robust": False, "models": ["a", "b"],
                                                  "agreement_ratio": 0.25, "note": "2 个模型 × 4 个 case：一致 1/4"}})
    out = run_report(tmp_path)
    m12 = out["metrics"]["#12"]
    assert m12["verdict"] == "warn"
    assert m12["data"]["intra_model_cost"]["tool_calls"]["n"] == 3  # 两信号分写
    assert m12["data"]["model_robust"]["agreement_ratio"] == 0.25
    assert "跨模型一致性" in m12["note"]


def test_model_robust_does_not_upgrade_single_run(tmp_path):
    write_inputs(tmp_path, {"score.json": stable_cost(n=1),
                            "model_robust.json": {"skipped": False, "robust": True, "models": ["a", "b"],
                                                  "agreement_ratio": 1.0, "note": "一致"}})
    out = run_report(tmp_path)
    assert out["metrics"]["#12"]["verdict"] == "warn"  # 单次运行不能靠跨模型洗成 pass


def test_model_robust_gives_12_evidence_without_cost_data(tmp_path):
    write_inputs(tmp_path, {"model_robust.json": {"skipped": False, "robust": True, "models": ["a", "b"],
                                                  "agreement_ratio": 1.0, "note": "一致"}})
    out = run_report(tmp_path)
    assert out["metrics"]["#12"]["verdict"] == "pass"
    assert out["metrics"]["#12"]["data"]["intra_model_cost"] is None


def test_model_robust_skipped_is_not_folded(tmp_path):
    write_inputs(tmp_path, {"score.json": stable_cost(),
                            "model_robust.json": {"skipped": True, "robust": None,
                                                  "note": "只有 1 个模型的 trace"}})
    out = run_report(tmp_path)
    assert out["metrics"]["#12"]["verdict"] == "pass"  # 与未启用跨模型时一致
    assert out["metrics"]["#12"]["data"]["tool_calls"]["n"] == 3


# --- #14 评估器指纹可比性（evolution）---

def make_result(d: Path, version: str, evaluator=None, meta_only=False):
    vdir = d / "results" / version
    vdir.mkdir(parents=True)
    report = {"metrics": {"#2": {"verdict": "pass", "method": "静态检查", "data": None, "note": ""}},
              "conclusion": "with-warnings"}
    if evaluator:
        report["evaluator"] = {"version": evaluator}
    (vdir / "report.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    if meta_only:
        (vdir / "meta.json").write_text(json.dumps({"evaluator": {"version": evaluator}}), encoding="utf-8")


def run_evo(d: Path):
    r = subprocess.run([sys.executable, str(EVO), str(d)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_same_fingerprint_is_comparable(tmp_path):
    make_result(tmp_path, "v1", evaluator="auto-aaaa1111")
    make_result(tmp_path, "v2", evaluator="auto-aaaa1111")
    out = run_evo(tmp_path)
    assert out["comparable"] is True
    assert "auto-aaaa1111" in out["evaluator_note"]


def test_different_fingerprint_not_comparable(tmp_path):
    make_result(tmp_path, "v1", evaluator="auto-aaaa1111")
    make_result(tmp_path, "v2", evaluator="auto-bbbb2222")
    out = run_evo(tmp_path)
    assert out["comparable"] is False
    assert out["evaluator_versions"] == {"v1": "auto-aaaa1111", "v2": "auto-bbbb2222"}
    assert "不构成" in out["evaluator_note"]


def test_missing_fingerprint_is_unknown(tmp_path):
    make_result(tmp_path, "v1")
    make_result(tmp_path, "v2")
    out = run_evo(tmp_path)
    assert out["comparable"] is None
    assert "可比性未知" in out["evaluator_note"]


def test_fingerprint_read_from_meta_json_fallback(tmp_path):
    # 旧报告可能没有 evaluator 字段，但 meta.json 有 → 仍要读出来
    make_result(tmp_path, "v1")
    make_result(tmp_path, "v2", evaluator="auto-cccc3333", meta_only=True)
    out = run_evo(tmp_path)
    assert out["evaluator_versions"]["v2"] == "auto-cccc3333"
    assert out["comparable"] is None  # v1 缺失 → 未知
