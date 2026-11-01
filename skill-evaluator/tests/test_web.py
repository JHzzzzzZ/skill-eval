"""web/app.py 的上传安全边界（不启动 HTTP 服务）。"""
import importlib.util
import inspect
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

WEB_APP = Path(__file__).parent.parent / "web" / "app.py"
spec = importlib.util.spec_from_file_location("web_app", WEB_APP)
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)


def test_zip_slip_is_rejected(tmp_path):
    z = tmp_path / "bad.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("../evil.txt", "x")
    with pytest.raises(ValueError):
        web._safe_extract(z, tmp_path / "out")


def test_ui_has_no_internal_adr_or_tier_jargon():
    html = (WEB_APP.parent / "index.html").read_text(encoding="utf-8")
    assert "ADR" not in html
    assert "T2B" not in html
    assert 'id="stop"' in html
    assert 'id="workspace"' in html and 'id="skill"' not in html
    assert 'id="realList"' in html and 'id="real"' not in html
    assert 'id="rerunT0"' in html and 'id="rerunT1"' in html and 'id="rerunAll"' in html
    assert 'id="reuse"' not in html
    assert 'verdict-picker' in html and '选中调整' not in html


def test_ui_section_eyebrows_are_chinese():
    html = (WEB_APP.parent / "index.html").read_text(encoding="utf-8")
    for english in ("Evaluation workspace", ">History<", ">Result<"):
        assert english not in html, english
    for chinese in ("<div class=\"eyebrow\">评估工作台</div>",
                    "<div class=\"eyebrow\">结果</div>",
                    "<div class=\"eyebrow\">历史</div>"):
        assert chinese in html, chinese


def test_stop_process_terminates():
    kwargs = {} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
    try:
        web._stop_process(proc)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()


class _ExitedProc:
    def poll(self):
        return 0


def test_stop_run_defers_stopped_event_to_api_stop(tmp_path):
    # 提前写 stopped 会让 SSE 拿到空 report 后关流，事件统一由 /api/stop 写
    rdir = tmp_path / "run"
    rdir.mkdir()
    (rdir / "run.json").write_text(json.dumps({"id": "r1"}), encoding="utf-8")
    progress = rdir / "progress.ndjson"
    progress.write_text(json.dumps({"type": "stage", "name": "t0", "status": "done"}) + "\n",
                        encoding="utf-8")
    run = {"proc": _ExitedProc(), "dir": rdir, "progress": progress, "stopped": False}
    web._stop_run(run)
    assert run["stopped"] is True
    assert json.loads((rdir / "run.json").read_text(encoding="utf-8"))["stopped"] is True
    types = [json.loads(line)["type"]
             for line in progress.read_text(encoding="utf-8").splitlines()]
    assert types == ["stage"]


def test_copy_reuse_only_copies_t0_t1_artifacts(tmp_path):
    prev = tmp_path / "prev"
    (prev / "judges").mkdir(parents=True)
    (prev / "raw" / "llm-judge").mkdir(parents=True)
    (prev / "static.json").write_text("{}", encoding="utf-8")
    (prev / "run-meta.json").write_text("{}", encoding="utf-8")
    (prev / "judges" / "brevity.json").write_text("{}", encoding="utf-8")
    (prev / "raw" / "llm-judge" / "x.json").write_text("{}", encoding="utf-8")
    (prev / "report.json").write_text("{}", encoding="utf-8")
    dest = tmp_path / "dest"
    assert web._copy_reuse(prev, dest) is True
    assert (dest / "static.json").is_file()
    assert (dest / "judges" / "brevity.json").is_file()
    assert (dest / "raw" / "llm-judge" / "x.json").is_file()
    assert not (dest / "report.json").exists()


def test_workspace_upsert_reuses_same_content(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "WORKSPACES", tmp_path / "workspaces")
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: demo\n---\nbody\n", encoding="utf-8")
    first = web._workspace_upsert(skill, "a.zip")
    second = web._workspace_upsert(skill, "b.zip")
    assert first["workspace_id"] == second["workspace_id"]
    assert first["skill"] == second["skill"]
    assert (Path(first["skill"]) / "SKILL.md").is_file()
    assert Path(first["evalset"]).parent.name == "demo-skill"
    assert Path(first["evalset"]).name == "v1"


def test_valid_zip_finds_skill_root(tmp_path):
    z = tmp_path / "ok.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("myskill/SKILL.md", "---\nname: s\ndescription: d\n---\nbody\n")
    dest = tmp_path / "out"
    web._safe_extract(z, dest)
    assert web._skill_root(dest) == dest / "myskill"


# --- #13/#15 评测集版本：自动识别 + 来源标记 ---

def make_evalset(root: Path, name="demo", version="v1", sha=None, frozen=False):
    d = root / name / version
    (d / "triggers" / "should").mkdir(parents=True, exist_ok=True)
    (d / "cases").mkdir(parents=True, exist_ok=True)
    (d / "triggers" / "should" / "s1.json").write_text(
        json.dumps({"prompt": "帮我评估"}), encoding="utf-8")
    (d / "cases" / "c1.json").write_text(
        json.dumps({"prompt": "p", "expect": "e"}), encoding="utf-8")
    meta = {"name": name, "version": version}
    if sha:
        meta["skill_sha256"] = sha
    if frozen:
        meta["frozen_at"] = "2026-01-01T00:00:00"
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


def test_evalset_versions_mark_current_old_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "EVALSETS", tmp_path / "evalsets")
    make_evalset(tmp_path / "evalsets", "demo", "v1", sha="old-sha")
    make_evalset(tmp_path / "evalsets", "demo", "v2", sha="current-sha")
    make_evalset(tmp_path / "evalsets", "demo", "v3")
    fields = web._evalset_fields("demo", "current-sha")
    assert fields["evalset_version"] == "v3"
    sources = {v["version"]: v["source"] for v in fields["evalset_versions"]}
    assert sources == {"v1": "old", "v2": "current", "v3": "unknown"}
    assert Path(fields["evalset"]).name == "v3"


def test_workspace_upsert_defaults_to_latest_evalset(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "WORKSPACES", tmp_path / "workspaces")
    monkeypatch.setattr(web, "EVALSETS", tmp_path / "evalsets")
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: versioned\ndescription: d\n---\nbody\n", encoding="utf-8")
    make_evalset(tmp_path / "evalsets", "versioned", "v1")
    first = web._workspace_upsert(skill, "a.zip")
    assert first["evalset_version"] == "v1"
    make_evalset(tmp_path / "evalsets", "versioned", "v2")
    second = web._workspace_upsert(skill, "a.zip")
    assert second["evalset_version"] == "v2"
    assert [v["version"] for v in second["evalset_versions"]] == ["v1", "v2"]


# --- #14 评测集导入/导出 ---

def test_evalset_export_import_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "EVALSETS", tmp_path / "evalsets")
    src = make_evalset(tmp_path / "evalsets", "demo", "v1", frozen=True)
    filename, blob = web._evalset_export_zip(src)
    assert filename == "evalset-demo-v1.zip"
    names = set(zipfile.ZipFile(io.BytesIO(blob)).namelist())
    assert {"meta.json", "triggers/should/s1.json", "cases/c1.json"} <= names

    monkeypatch.setattr(web, "EVALSETS", tmp_path / "imported")
    root = tmp_path / "unzip"
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        zf.extractall(root)
    first = web._import_evalset(root, filename)
    assert first["evalset_version"] == "v1"
    meta = json.loads((Path(first["evalset"]) / "meta.json").read_text(encoding="utf-8"))
    assert meta["imported_at"] and meta["source_archive"] == filename

    second = web._import_evalset(root, filename)
    assert second["evalset_version"] == "v2"      # 已存在 v1 → 自动新建，绝不覆盖
    assert (tmp_path / "imported" / "demo" / "v1" / "triggers" / "should" / "s1.json").is_file()
    assert (tmp_path / "imported" / "demo" / "v2" / "cases" / "c1.json").is_file()


def test_evalset_export_rejects_outside_evalsets(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "EVALSETS", tmp_path / "evalsets")
    outside = make_evalset(tmp_path, "demo", "v1")
    with pytest.raises(ValueError):
        web._evalset_export_zip(outside)


def test_evalset_import_requires_triggers_or_cases(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "EVALSETS", tmp_path / "evalsets")
    root = tmp_path / "empty"
    root.mkdir()
    (root / "meta.json").write_text('{"name": "x"}', encoding="utf-8")
    with pytest.raises(ValueError, match="触发组或 cases"):
        web._import_evalset(root, "x.zip")


def test_evalset_extract_limits_file_count(tmp_path):
    z = tmp_path / "many.zip"
    with zipfile.ZipFile(z, "w") as f:
        for i in range(4):
            f.writestr(f"triggers/should/s{i}.json", "{}")
    with pytest.raises(ValueError, match="文件数"):
        web._safe_extract(z, tmp_path / "out", max_files=2)


# --- #20 历史运行：磁盘回退 ---

def write_run(root: Path, rid="abc123def", tier="t2a", conclusion="with-warnings",
              partial=False, stopped=False):
    rdir = root / rid
    results = rdir / "results"
    results.mkdir(parents=True)
    (rdir / "run.json").write_text(json.dumps({
        "id": rid, "tier": tier, "skill_name": "demo", "stopped": stopped,
        "created_at": "2026-11-01T10:00:00"}), encoding="utf-8")
    (results / "run-meta.json").write_text(json.dumps({
        "skill": str(root / "skill"), "tier": tier}), encoding="utf-8")
    report = {"conclusion": conclusion, "metrics": {}, "partial": partial}
    if partial:
        report["completed_stages"] = ["t0"]
    (results / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (results / "report.html").write_text("<html></html>", encoding="utf-8")
    (rdir / "run.log").write_text("\n".join(f"line {i}" for i in range(250)), encoding="utf-8")
    return rdir


def test_disk_status_report_and_log_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path / "runs")
    write_run(tmp_path / "runs", stopped=True, partial=True)
    status = web._status_payload("abc123def", None)
    assert status["running"] is False
    assert status["stopped"] is True and status["partial"] is True
    assert status["completed_stages"] == ["t0"]
    assert status["report"].endswith("report.html")
    runs = web._runs()
    assert runs[0]["id"] == "abc123def"
    assert runs[0]["conclusion"] == "with-warnings"
    assert runs[0]["has_report"] is True
    tail = web._tail_log("abc123def")
    assert tail.splitlines()[0] == "line 50"      # 尾部 200 行
    assert len(tail.splitlines()) == 200
    assert web._disk_status("missing") is None
    with pytest.raises(ValueError):
        web._tail_log("missing")


# --- #25 停止后的局部报告 ---

def write_static(results: Path):
    results.mkdir(parents=True, exist_ok=True)
    (results / "static.json").write_text(json.dumps({
        "name": "demo", "description": "d", "invoke": {"resolved": "both"},
        "errors": [], "warnings": [], "passed": True}), encoding="utf-8")


def write_judges(results: Path):
    jdir = results / "judges"
    jdir.mkdir(parents=True, exist_ok=True)
    for metric in ("brevity", "redundancy", "fallback", "precheck", "contract", "deps", "pii"):
        (jdir / f"{metric}.json").write_text(json.dumps({
            "items": [{"name": "x", "pass": True, "quote": "x"}],
            "score": 1.0, "evidence": [], "reason": ""}), encoding="utf-8")


class _RunningProc:
    def poll(self):
        return None


def test_runs_active_entry_uses_process_status(tmp_path, monkeypatch):
    # ACTIVE 的运行还在跑：_runs 不能走 _disk_status 误报 running:false
    monkeypatch.setattr(web, "RUNS", tmp_path / "runs")
    rdir = write_run(tmp_path / "runs")
    monkeypatch.setitem(web.ACTIVE, "abc123def", {
        "proc": _RunningProc(), "dir": rdir, "results": rdir / "results"})
    runs = web._runs()
    assert runs[0]["id"] == "abc123def"
    assert runs[0]["running"] is True
    assert runs[0]["has_report"] is True
    assert runs[0]["conclusion"] == "with-warnings"


def test_completed_stages_and_partial_report(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path / "runs")
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nbody\n",
                                    encoding="utf-8")
    rdir = tmp_path / "runs" / "run1"
    results = rdir / "results"
    write_static(results)
    write_judges(results)
    (results / "triggers").mkdir()
    (results / "triggers" / "should-1.json").write_text("{}", encoding="utf-8")  # 半截触发
    (results / "run-meta.json").write_text(json.dumps({
        "skill": str(skill), "tier": "t2a"}), encoding="utf-8")

    assert web._completed_stages(results) == ["t0", "t1"]
    out = web._finalize_partial({"results": results, "dir": rdir})
    assert out["ok"] is True, out
    assert out["completed_stages"] == ["t0", "t1"]
    report = json.loads((results / "report.json").read_text(encoding="utf-8"))
    assert report["partial"] is True
    assert report["metrics"]["#10"]["verdict"] == "skipped"
    assert report["metrics"]["#5"]["verdict"] != "skipped"
    assert (results / "report.html").is_file()

    # 人工调整/审核刷新重出报告：局部报告不能变回“完整”
    again = web._rerun_report({"results": results})
    assert again["partial"] is True
    assert again["completed_stages"] == ["t0", "t1"]


def test_stop_without_t0_does_not_write_empty_report(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path / "runs")
    rdir = tmp_path / "runs" / "run2"
    results = rdir / "results"
    results.mkdir(parents=True)
    out = web._finalize_partial({"results": results, "dir": rdir})
    assert out["ok"] is False
    assert "基础检查" in out["error"]
    assert not (results / "report.json").exists()


def test_events_has_no_hard_deadline_and_pings():
    src = inspect.getsource(web.Handler._events)
    assert "60 * 60" not in src
    assert ": ping" in src


# --- 前端接线 ---

def test_ui_wires_versions_history_and_error_ui():
    html = (WEB_APP.parent / "index.html").read_text(encoding="utf-8")
    assert 'id="evalVersion"' in html and 'id="evalOut"' not in html
    assert "评测集版本" in html
    assert 'id="errorBar"' in html and 'id="logModal"' in html
    assert 'id="recent"' in html and 'id="errorLog"' in html
    assert "全部通过" in html and "有警告" in html and "有失败" in html
    assert "已完成的阶段会保留，并生成局部报告" in html
    assert "/api/runs" in html and "/api/log" in html
    assert "/api/evalset/import" in html and "/api/evalset/export" in html
    assert "position:sticky" in html.replace(" ", "")




def test_ui_tier_label_accepts_lowercase_completed_stages():
    html = (WEB_APP.parent / "index.html").read_text(encoding="utf-8")
    assert "function tierLabel" in html
    assert "TIER_LABELS[t]||t" not in html
    assert "map(tierLabel)" in html
    for label in ("基础检查", "文档语义评审", "触发评测", "真实输入对比", "完整任务运行"):
        assert label in html


def test_run_id_path_traversal_is_rejected():
    assert web._run_dir("../secret") is None
    assert web._run_dir("") is None
    assert web._results_dir("../secret") is None
    with pytest.raises(ValueError):
        web._tail_log("../secret")
