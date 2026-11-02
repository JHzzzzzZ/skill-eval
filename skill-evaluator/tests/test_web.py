"""web/app.py 与前端接线的回归（后台服务只在审核人 400 用例里临时启动）。"""
import http.client
import importlib.util
from contextlib import contextmanager
import inspect
import io
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

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


# --- 内容保真 / provenance / 审核人 / 预检 ---

def test_ui_public_text_keeps_user_content():
    html = (WEB_APP.parent / "index.html").read_text(encoding="utf-8")
    start = html.index("function publicText")
    body = html[start:start + 220]
    assert "A[D]R" not in body and "ADR" not in body
    assert "replace(/\\s{2,}/g" in body


def test_report_payload_adds_run_meta_and_run_id(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path / "runs")
    results = write_run(tmp_path / "runs") / "results"
    code, payload = web._report_payload("abc123def")
    assert code == 200
    assert payload["run_id"] == "abc123def"
    assert payload["run_meta"]["tier"] == "t2a"
    assert "run_id" not in json.loads((results / "report.json").read_text(encoding="utf-8"))
    assert web._report_payload("missing")[0] == 404


def test_override_rejects_empty_reviewer():
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request("POST", "/api/override",
                     body=json.dumps({"id": "x", "reviewer": "  ", "reason": "r"}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        assert resp.status == 400
        assert "审核人" in data["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_ui_wires_evidence_provenance_reviewer_preflight():
    html = (WEB_APP.parent / "index.html").read_text(encoding="utf-8")
    for element in ('id="evidenceModal"', 'id="provenance"', 'id="reviewerName"',
                    'id="timing"', 'id="notifyDone"'):
        assert element in html, element
    assert "preflightWarnings" in html and "refreshRunHint" in html
    assert "const WEIGHT" not in html and "STAGE_WEIGHTS" in html
    assert "progressValue = Math.max" in html
    assert 'localStorage.getItem("se-reviewer")' in html
    assert "document.title" in html and "Notification" in html
    assert "/api/report?id=" in html


# --- B1 评测集前置 ---

def test_ui_evalset_card_precedes_tier_card_and_shows_prereq():
    html = (WEB_APP.parent / "index.html").read_text(encoding="utf-8")
    assert html.index("02 · 评测集（按需）") < html.index("03 · 评估深度")
    assert html.index('id="evalCard"') < html.index('id="tierGrid"')
    assert 'id="runPrereq"' in html
    assert "未创建，将跳过 #10" in html
    assert "评测集不参与本次评估" in html and "没有真实输入，将跳过" in html
    assert "function refreshRunPrereq" in html
    assert "selectedEvalsetVersion" in html


# --- B2 历史与对比 ---

def metric(verdict, note=""):
    return {"verdict": verdict, "method": "static", "note": note, "data": {}}


def write_compare_run(root: Path, rid: str, *, metrics: dict, skill_sha: str = "skill-a",
                      evaluator_sha: "str | None" = "eval-a", schema: int = 2,
                      created_at: str = "2026-11-01T10:00:00") -> Path:
    rdir = root / rid
    results = rdir / "results"
    results.mkdir(parents=True)
    (rdir / "run.json").write_text(json.dumps({
        "id": rid, "tier": "t2a", "skill_name": "demo", "created_at": created_at}),
        encoding="utf-8")
    (results / "run-meta.json").write_text(json.dumps({
        "skill": str(root / "skill"), "skill_name": "demo", "tier": "t2a",
        "skill_sha256": skill_sha, "created_at": created_at}), encoding="utf-8")
    report = {"schema": schema, "conclusion": "with-warnings", "metrics": metrics}
    if evaluator_sha is not None:
        report["evaluator"] = {"version": "auto-" + evaluator_sha[:8],
                               "content_sha256": evaluator_sha}
    (results / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (results / "report.md").write_text("# demo\n", encoding="utf-8")
    (results / "report.html").write_text("<html></html>", encoding="utf-8")
    (results / "meta.json").write_text(json.dumps({"schema": "skill-eval/results-meta/1"}),
                                       encoding="utf-8")
    (rdir / "run.log").write_text("log line", encoding="utf-8")
    (results / "traces").mkdir()
    (results / "traces" / "t1.json").write_text("{}", encoding="utf-8")
    (results / "judges").mkdir()
    (results / "judges" / "brevity.json").write_text('{"score":1}', encoding="utf-8")
    (results / "triggers").mkdir()
    (results / "triggers" / "should-1.json").write_text('{"steps":[]}', encoding="utf-8")
    (results / "score.json").write_text('{"trigger":"pass"}', encoding="utf-8")
    return rdir


def test_compare_reports_marks_improved_regressed_and_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path / "runs")
    write_compare_run(tmp_path / "runs", "base1", metrics={
        "#1": metric("fail"), "#2": metric("warn", "old"),
        "#3": metric("pass"), "#4": metric("skipped")})
    write_compare_run(tmp_path / "runs", "target1", metrics={
        "#1": metric("pass"), "#2": metric("fail", "new"),
        "#3": metric("pass"), "#4": metric("skipped")})
    code, data = web._compare_reports("base1", "target1")
    assert code == 200
    assert data["comparable"] is True and data["skill_same"] is True
    assert data["base"]["id"] == "base1" and data["target"]["id"] == "target1"
    assert data["metrics"]["#1"]["change"] == "improved"
    assert data["metrics"]["#2"]["change"] == "regressed"
    assert data["metrics"]["#2"]["base_note"] == "old"
    assert data["metrics"]["#2"]["target_note"] == "new"
    assert data["metrics"]["#3"]["change"] == "same"
    assert data["metrics"]["#4"]["change"] == "skipped"
    assert data["counts"] == {"improved": 1, "regressed": 1, "same": 1, "skipped": 1}
    assert list(data["metrics"]) == ["#1", "#2", "#3", "#4"]
    assert data["notes"] == []


def test_compare_flags_incomparable_and_different_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path / "runs")
    write_compare_run(tmp_path / "runs", "base1", metrics={"#1": metric("pass")})
    write_compare_run(tmp_path / "runs", "target1", metrics={"#1": metric("pass")},
                      skill_sha="skill-b", evaluator_sha="eval-b", schema=3)
    code, data = web._compare_reports("base1", "target1")
    assert code == 200
    assert data["comparable"] is False
    assert data["skill_known"] is True and data["skill_same"] is False
    assert any("不可比" in note for note in data["notes"])
    assert any("skill" in note for note in data["notes"])


def test_compare_requires_evaluator_fingerprint_on_both_sides(tmp_path, monkeypatch):
    """旧报告缺评估器指纹时 None == None 不能算可比（不可作为回归证据）。"""
    monkeypatch.setattr(web, "RUNS", tmp_path / "runs")
    write_compare_run(tmp_path / "runs", "base1", metrics={"#1": metric("pass")},
                      evaluator_sha=None)
    write_compare_run(tmp_path / "runs", "target1", metrics={"#1": metric("fail")},
                      evaluator_sha=None)
    code, data = web._compare_reports("base1", "target1")
    assert code == 200
    assert data["comparable"] is False
    assert any("不可比" in note for note in data["notes"])
    # 缺指纹不影响指标表本身，只是不能当回归证据
    assert data["metrics"]["#1"]["change"] == "regressed"


def test_compare_different_skill_same_evaluator_still_comparable(tmp_path, monkeypatch):
    """不同版本/不同 skill 仍按 evaluator 指纹判断可比，只是 skill_same=False。"""
    monkeypatch.setattr(web, "RUNS", tmp_path / "runs")
    write_compare_run(tmp_path / "runs", "base1", metrics={"#1": metric("fail")},
                      skill_sha="skill-a", evaluator_sha="eval-a")
    write_compare_run(tmp_path / "runs", "target1", metrics={"#1": metric("pass")},
                      skill_sha="skill-b", evaluator_sha="eval-a")
    code, data = web._compare_reports("base1", "target1")
    assert code == 200
    assert data["comparable"] is True
    assert data["skill_known"] is True and data["skill_same"] is False
    assert data["metrics"]["#1"] == {"base": "fail", "target": "pass",
                                     "change": "improved", "base_note": "",
                                     "target_note": ""}
    assert any("不同 skill" in note for note in data["notes"])


def test_compare_requires_ids_and_existing_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path / "runs")
    write_compare_run(tmp_path / "runs", "base1", metrics={})
    assert web._compare_reports("", "base1")[0] == 400
    assert web._compare_reports("base1", "../secret")[0] == 400
    assert web._compare_reports("base1", "missing")[0] == 404


def test_ui_wires_history_and_compare_modals():
    html = (WEB_APP.parent / "index.html").read_text(encoding="utf-8")
    for element in ('id="historyOpen"', 'id="historyModal"', 'id="historyList"',
                    'id="compareBase"', 'id="compareTarget"', 'id="compareGo"',
                    'id="compareModal"', 'id="compareBody"'):
        assert element in html, element
    assert "查看全部历史" in html and "对比选中" in html
    assert "/api/runs?limit=100" in html and "/api/compare?base=" in html
    assert "不可比" in html and "不同 skill" in html
    # comparable 只由后端裁决决定；skill_same 只影响横幅提示，不能吞掉指标表
    assert "const comparable = !!data.comparable;" in html
    assert "!!data.comparable && !!data.skill_same" not in html
    assert "不同 skill / 不同版本，仅供参考" in html


# --- B3 导出 / 分享 ---

def test_export_zip_contains_report_and_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path / "runs")
    write_compare_run(tmp_path / "runs", "run1", metrics={"#1": metric("pass")})
    filename, blob = web._export_zip("run1")
    assert filename == "report-run1.zip"
    names = set(zipfile.ZipFile(io.BytesIO(blob)).namelist())
    assert {"report.json", "report.md", "report.html", "meta.json",
            "score.json", "judges/brevity.json", "triggers/should-1.json",
            "traces/t1.json"} <= names
    assert "run.log" not in names
    assert not any(name.startswith("sandbox/") for name in names)


def test_export_rejects_traversal_and_missing_report(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path / "runs")
    write_compare_run(tmp_path / "runs", "run1", metrics={})
    with pytest.raises(ValueError):
        web._export_zip("../secret")
    with pytest.raises(FileNotFoundError):
        web._export_zip("missing")


def test_ui_wires_export_and_copy_link():
    html = (WEB_APP.parent / "index.html").read_text(encoding="utf-8")
    assert 'id="exportLink"' in html and 'id="copyLink"' in html
    assert "导出报告 .zip" in html and "复制链接" in html
    assert "/api/export?id=" in html
    assert 'location.origin + location.pathname + "?run="' in html

# --- 分层访问与审计：T2/T3 token 门禁、游客 T0/T1、run 归属、audit.jsonl ---

class _FakeProc:
    """不真跑 runner：只验证门禁与归属，进程保持“在跑”。"""

    def __init__(self, cmd=None, **kwargs):
        self.cmd = list(cmd or [])
        self.pid = 0

    def poll(self):
        return None


def allow_capability(tier, *, adapter=None, isolation=None, sandbox_status=None):
    """能力检查的测试桩：默认环境可用（容器类用例各自覆盖）。"""
    return {"tier": tier, "isolation": isolation or web.capabilities.default_isolation(tier),
            "ok": True, "missing": [], "reasons": [], "fix": [], "warnings": [],
            "container": None,
            "adapter": {"name": adapter or "pi", "available": True, "reason": ""}}


def fake_capability_snapshot(*, adapter=None, sandbox_status=None):
    return {"tiers": {tier: allow_capability(tier, adapter=adapter)
                      for tier in web.capabilities.TIERS},
            "container": {"engine": "docker", "available": True, "reason": ""},
            "adapter": {"name": adapter or "pi", "available": True, "reason": ""},
            "adapters": [{"name": "pi", "available": True, "reason": ""},
                         {"name": "claude-code", "available": False,
                          "reason": "未安装"}],
            "model_proxy": {"configured": False, "note": "模型代理尚未实现（测试桩）"}}


def write_owned_run(root: Path, rid: str, *, owner_kind="guest", owner_id="", protected=False,
                    tier="t2a") -> Path:
    rdir = root / rid
    results = rdir / "results"
    results.mkdir(parents=True)
    run_json = {"id": rid, "tier": tier, "skill_name": "demo",
                "created_at": "2026-11-01T10:00:00", "skill_sha256": "sha-1"}
    if owner_kind:
        run_json.update({"owner_kind": owner_kind, "owner_id": owner_id,
                         "owner_name": owner_id, "protected": protected})
    (rdir / "run.json").write_text(json.dumps(run_json), encoding="utf-8")
    (results / "run-meta.json").write_text(json.dumps({
        "skill": str(root / "skill"), "skill_name": "demo", "tier": tier,
        "skill_sha256": "sha-1"}), encoding="utf-8")
    (results / "report.json").write_text(json.dumps(
        {"conclusion": "with-warnings", "metrics": {}}), encoding="utf-8")
    (results / "report.html").write_text("<html></html>", encoding="utf-8")
    (rdir / "run.log").write_text("line", encoding="utf-8")
    return rdir


@contextmanager
def web_server(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS", tmp_path / "runs")
    (tmp_path / "runs").mkdir(exist_ok=True)
    monkeypatch.setattr(web, "ACTIVE", {})
    monkeypatch.setattr(web, "SINGLE_USER", False)
    monkeypatch.setattr(web.token_admin, "TOKENS_FILE", tmp_path / "tokens.json")
    monkeypatch.setattr(web.token_admin, "AUDIT_FILE", tmp_path / "audit.jsonl")
    monkeypatch.setattr(web.ratelimit, "DB", tmp_path / "usage.sqlite3")
    monkeypatch.setattr(web.ratelimit, "LIMITS_FILE", tmp_path / "limits.json")
    monkeypatch.setattr(web.capabilities, "check", allow_capability)
    monkeypatch.setattr(web.capabilities, "snapshot", fake_capability_snapshot)
    monkeypatch.setattr(web.subprocess, "Popen", _FakeProc)
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def request(server, method, path, body=None, headers=None, cookie=""):
    """返回 (status, json, 响应头, Set-Cookie 首段)；call() 是它的三元组包装。"""
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    hdrs = dict(headers or {})
    payload = None
    if body is not None:
        payload = json.dumps(body)
        hdrs.setdefault("Content-Type", "application/json")
    if cookie:
        hdrs["Cookie"] = cookie
    conn.request(method, path, body=payload, headers=hdrs)
    resp = conn.getresponse()
    raw = resp.read()
    resp_headers = {key.lower(): value for key, value in resp.getheaders()}
    set_cookie = resp.getheader("Set-Cookie") or ""
    conn.close()
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    return resp.status, data, resp_headers, set_cookie.split(";", 1)[0]


def call(server, method, path, body=None, headers=None, cookie=""):
    status, data, _headers, set_cookie = request(server, method, path, body, headers, cookie)
    return status, data, set_cookie


def make_skill(tmp_path: Path) -> Path:
    skill = tmp_path / "skill"
    skill.mkdir(exist_ok=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nbody\n",
                                    encoding="utf-8")
    return skill


def test_guests_can_run_t0_t1_but_t2a_needs_token(tmp_path, monkeypatch):
    skill = make_skill(tmp_path)
    with web_server(tmp_path, monkeypatch) as server:
        status, data, cookie = call(server, "POST", "/api/run",
                                    {"skill": str(skill), "tier": "t2a"})
        assert status == 401 and "token" in data["error"]
        assert cookie.startswith("se_guest=")
        assert not list((tmp_path / "runs").glob("*/run.json"))   # 服务端不启动
        guest = cookie.split("=", 1)[1]

        for tier in ("t0", "t1"):
            status, data, _ = call(server, "POST", "/api/run",
                                   {"skill": str(skill), "tier": tier}, cookie=cookie)
            assert status == 200, data
            run_meta = json.loads((tmp_path / "runs" / data["id"] / "run.json")
                                  .read_text(encoding="utf-8"))
            assert run_meta["owner_kind"] == "guest"
            assert run_meta["owner_id"] == guest
            assert run_meta["owner_name"] == "游客"
            assert run_meta["protected"] is False
            assert run_meta["tier"] == tier and run_meta["skill_sha256"]
            # 假进程一直 running：不清掉会被游客并发额度（1）拒绝下一次
            web.ACTIVE.clear()


def test_t2a_with_token_is_owner_scoped(tmp_path, monkeypatch):
    skill = make_skill(tmp_path)
    with web_server(tmp_path, monkeypatch) as server:
        owner = web.token_admin.issue("张三")
        other = web.token_admin.issue("李四")
        owner_h = {"X-Skill-Eval-Token": owner["token"]}
        other_h = {"X-Skill-Eval-Token": other["token"]}

        status, data, _ = call(server, "POST", "/api/run",
                               {"skill": str(skill), "tier": "t2a"}, headers=owner_h)
        assert status == 200, data
        rid = data["id"]
        run_meta = json.loads((tmp_path / "runs" / rid / "run.json").read_text(encoding="utf-8"))
        assert run_meta["owner_kind"] == "token" and run_meta["owner_id"] == owner["id"]
        assert run_meta["owner_name"] == "张三" and run_meta["protected"] is True

        for path in (f"/api/status?id={rid}", f"/api/report?id={rid}", f"/api/log?id={rid}",
                     f"/api/export?id={rid}", f"/report?id={rid}", f"/api/events?id={rid}"):
            assert call(server, "GET", path, headers=other_h)[0] == 403, path
            assert call(server, "GET", path, cookie="se_guest=" + "a" * 32)[0] == 403, path
        assert call(server, "POST", "/api/stop", {"id": rid}, headers=other_h)[0] == 403
        assert call(server, "POST", "/api/override", {"id": rid}, headers=other_h)[0] == 403
        assert call(server, "GET", f"/api/status?id={rid}", headers=owner_h)[0] == 200


def test_single_user_sees_guest_and_legacy_but_not_token_runs(tmp_path, monkeypatch):
    with web_server(tmp_path, monkeypatch) as server:
        monkeypatch.setattr(web, "SINGLE_USER", True)
        runs = tmp_path / "runs"
        write_owned_run(runs, "guestrun", owner_kind="guest", owner_id="a" * 32, tier="t0")
        write_owned_run(runs, "legacyrun", owner_kind=None, tier="t0")
        write_owned_run(runs, "tokenrun", owner_kind="token", owner_id="tok_x", protected=True)
        status, data, _ = call(server, "GET", "/api/runs?limit=50")
        assert status == 200
        ids = {r["id"] for r in data["runs"]}
        assert {"guestrun", "legacyrun"} <= ids and "tokenrun" not in ids
        assert call(server, "GET", "/api/status?id=guestrun")[0] == 200
        assert call(server, "GET", "/api/status?id=legacyrun")[0] == 200
        assert call(server, "GET", "/api/status?id=tokenrun")[0] == 403


def test_single_user_t2_does_not_require_token(tmp_path, monkeypatch):
    skill = make_skill(tmp_path)
    with web_server(tmp_path, monkeypatch) as server:
        monkeypatch.setattr(web, "SINGLE_USER", True)
        status, data, _ = call(server, "POST", "/api/run",
                               {"skill": str(skill), "tier": "t2a"})
        assert status == 200, data
        meta = json.loads((tmp_path / "runs" / data["id"] / "run.json")
                          .read_text(encoding="utf-8"))
        assert meta["owner_kind"] == "local" and meta["owner_id"] == "local"


def test_run_lists_and_legacy_runs_follow_ownership(tmp_path, monkeypatch):
    with web_server(tmp_path, monkeypatch) as server:
        owner = web.token_admin.issue("张三")
        owner_h = {"X-Skill-Eval-Token": owner["token"]}
        runs = tmp_path / "runs"
        write_owned_run(runs, "tokenrun", owner_kind="token", owner_id=owner["id"],
                        protected=True)
        write_owned_run(runs, "guestrun", owner_kind="guest", owner_id="a" * 32, tier="t0")
        write_owned_run(runs, "legacyrun", owner_kind=None)

        assert call(server, "GET", "/api/status?id=guestrun", headers=owner_h)[0] == 403
        assert call(server, "GET", "/api/status?id=guestrun",
                    cookie="se_guest=" + "a" * 32)[0] == 200
        assert call(server, "GET", "/api/status?id=guestrun",
                    cookie="se_guest=" + "b" * 32)[0] == 403
        # token 库非空 → legacy run 不属于任何身份，既不可读也不列出
        assert call(server, "GET", "/api/status?id=legacyrun",
                    cookie="se_guest=" + "b" * 32)[0] == 403
        assert call(server, "GET", "/api/report?id=legacyrun", headers=owner_h)[0] == 403

        status, data, _ = call(server, "GET", "/api/runs?limit=50",
                               cookie="se_guest=" + "a" * 32)
        assert {r["id"] for r in data["runs"]} == {"guestrun"}
        status, data, _ = call(server, "GET", "/api/runs?limit=50",
                               cookie="se_guest=" + "b" * 32)
        assert data["runs"] == []
        status, data, _ = call(server, "GET", "/api/runs?limit=50", headers=owner_h)
        assert {r["id"] for r in data["runs"]} == {"tokenrun"}
        # 跨 run 对比同样校验双方归属
        assert call(server, "GET", "/api/compare?base=guestrun&target=tokenrun",
                    headers=owner_h)[0] == 403


def test_legacy_runs_visible_only_without_tokens(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    with web_server(tmp_path, monkeypatch) as server:
        write_owned_run(runs, "legacyrun", owner_kind=None)
        bare = runs / "norunjson"        # 旧 run：目录和 results 在，但没有 run.json
        (bare / "results").mkdir(parents=True)
        (bare / "results" / "run-meta.json").write_text(
            json.dumps({"skill_name": "demo", "tier": "t0"}), encoding="utf-8")
        (bare / "run.log").write_text("line", encoding="utf-8")
        assert web._run_owner("norunjson")["kind"] == "legacy"

        guest = "se_guest=" + "a" * 32
        # 完全没有 token：单用户迁移期，legacy run 对当前身份放行
        for rid in ("legacyrun", "norunjson"):
            assert call(server, "GET", f"/api/status?id={rid}", cookie=guest)[0] == 200
            assert call(server, "GET", f"/api/log?id={rid}", cookie=guest)[0] == 200
        status, data, _ = call(server, "GET", "/api/runs?limit=50", cookie=guest)
        assert {r["id"] for r in data["runs"]} == {"legacyrun", "norunjson"}

        # 发过一个 token 后就是多用户模式：legacy 一律不可见/不可读
        token = web.token_admin.issue("张三")
        token_h = {"X-Skill-Eval-Token": token["token"]}
        for rid in ("legacyrun", "norunjson"):
            assert call(server, "GET", f"/api/status?id={rid}", cookie=guest)[0] == 403
            assert call(server, "GET", f"/api/report?id={rid}", headers=token_h)[0] == 403
        for who in ({"cookie": guest}, {"headers": token_h}):
            status, data, _ = call(server, "GET", "/api/runs?limit=50", **who)
            assert data["runs"] == []


def test_reuse_from_requires_ownership(tmp_path, monkeypatch):
    skill = make_skill(tmp_path)
    audit = tmp_path / "audit.jsonl"
    with web_server(tmp_path, monkeypatch) as server:
        owner = web.token_admin.issue("张三")
        other = web.token_admin.issue("李四")
        owner_h = {"X-Skill-Eval-Token": owner["token"]}
        other_h = {"X-Skill-Eval-Token": other["token"]}

        status, data, _ = call(server, "POST", "/api/run",
                               {"skill": str(skill), "tier": "t2a"}, headers=owner_h)
        assert status == 200, data
        src = data["id"]
        before = {p.name for p in (tmp_path / "runs").iterdir()}

        status, data, _ = call(server, "POST", "/api/run",
                               {"skill": str(skill), "tier": "t2a", "reuse_from": src},
                               headers=other_h)
        assert status == 403 and "复用" in data["error"]
        assert {p.name for p in (tmp_path / "runs").iterdir()} == before   # 拒绝时不建 run
        denied = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()
                  if json.loads(line)["action"] == "reuse_denied"]
        assert denied and denied[-1]["run_id"] == src
        assert denied[-1]["actor_id"] == other["id"] and denied[-1]["status"] == 403

        status, data, _ = call(server, "POST", "/api/run",
                               {"skill": str(skill), "tier": "t2a", "reuse_from": src},
                               headers=owner_h)
        assert status == 200 and data["id"] != src      # 自己的 run 可复用


def test_evalset_endpoints_require_token(tmp_path, monkeypatch):
    with web_server(tmp_path, monkeypatch) as server:
        for path in ("/api/evalset/status?evalset=x&skill=y", "/api/evalset/items?evalset=x",
                     "/api/evalset/export?evalset=x"):
            assert call(server, "GET", path)[0] == 401, path
        for path in ("/api/evalset/generate", "/api/evalset/review"):
            assert call(server, "POST", path, {})[0] == 401, path
        assert call(server, "POST", "/api/evalset/import?filename=x.zip")[0] == 401

        token = web.token_admin.issue("张三")
        status, _, _ = call(server, "GET", "/api/evalset/items?evalset=missing-evalset",
                            headers={"X-Skill-Eval-Token": token["token"]})
        assert status == 400      # 过了门禁，只剩业务错误


def test_token_check_reports_identity_and_revocation(tmp_path, monkeypatch):
    with web_server(tmp_path, monkeypatch) as server:
        assert call(server, "GET", "/api/token/check")[0] == 401
        token = web.token_admin.issue("张三")
        status, data, _ = call(server, "GET", "/api/token/check",
                               headers={"X-Skill-Eval-Token": token["token"]})
        assert status == 200
        assert data == {"ok": True, "id": token["id"], "name": "张三"}
        web.token_admin.revoke(token["id"])
        assert call(server, "GET", "/api/token/check",
                    headers={"X-Skill-Eval-Token": token["token"]})[0] == 401


AUDIT_FIELDS = {"ts", "ip", "x_forwarded_for", "user_agent", "actor_kind", "actor_id",
                "actor_name", "action", "method", "path", "status", "run_id", "tier",
                "skill_sha256", "detail"}


def test_audit_records_ip_ua_actor_without_token(tmp_path, monkeypatch):
    skill = make_skill(tmp_path)
    audit = tmp_path / "audit.jsonl"
    with web_server(tmp_path, monkeypatch) as server:
        token = web.token_admin.issue("张三")
        headers = {"X-Skill-Eval-Token": token["token"], "User-Agent": "pytest-agent/1.0",
                   "X-Forwarded-For": "203.0.113.9"}
        status, data, _ = call(server, "POST", "/api/run",
                               {"skill": str(skill), "tier": "t0"}, headers=headers)
        assert status == 200
        lines = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        start = [e for e in lines if e["action"] == "run_start"][0]
        assert start["actor_kind"] == "token" and start["actor_id"] == token["id"]
        assert start["actor_name"] == "张三"
        assert start["ip"] and start["user_agent"] == "pytest-agent/1.0"
        assert start["x_forwarded_for"] == "203.0.113.9"      # 只记录，不用于授权
        assert start["tier"] == "t0" and start["run_id"] == data["id"]
        assert start["skill_sha256"] and start["method"] == "POST"
        assert start["path"] == "/api/run"
        raw = audit.read_text(encoding="utf-8")
        assert token["token"] not in raw                        # 明文 token 不入审计
        assert all(set(entry) == AUDIT_FIELDS for entry in lines)


def test_ui_token_gate_hides_evalset_and_sends_header():
    html = (WEB_APP.parent / "index.html").read_text(encoding="utf-8")
    for element in ('id="accessToken"', 'id="tokenBtn"', 'id="tokenState"', 'id="evalCard"'):
        assert element in html, element
    assert "X-Skill-Eval-Token" in html
    assert 'localStorage.getItem("se-token")' in html
    assert 'const authorized = singleUser || tokenAuthorized;' in html
    assert '$("evalCard").hidden = !authorized' in html     # 游客隐藏评测集卡；本机模式可见
    assert "T2/T3 需要申请 token" in html
    assert "new EventSource" not in html                         # SSE 改 fetch 才能带请求头
    assert "headers: authHeaders()" in html
    assert 'fetch("/api/export?id=" + encodeURIComponent(rid), {headers: authHeaders()})' in html
    assert "blobDownload(await r.blob()" in html


# --- 限流：游客 T1 额度、token 覆盖、并发、429 不建 run、审计 ---

def usage_rows(tmp_path: Path) -> int:
    """读限流计数库的行数（测试直连，验证“拒绝不消耗额度”）。"""
    conn = sqlite3.connect(str(tmp_path / "usage.sqlite3"))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0])
    finally:
        conn.close()


def patch_token_quota(path: Path, token_id: str, **fields) -> None:
    """给已签发的 token 记录补配额字段（issue 不签发配额，限流只读取）。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    for record in data["tokens"]:
        if record["id"] == token_id:
            record.update(fields)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_limits_example_matches_code_defaults():
    # 入库样例是给人改的模板；与代码默认漂移会让“抄样例”改出意料外的额度
    example = json.loads((WEB_APP.parent / "limits.example.json").read_text(encoding="utf-8"))
    assert example == web.ratelimit.DEFAULTS


def test_guest_t1_quota_returns_429_without_creating_run(tmp_path, monkeypatch):
    skill = make_skill(tmp_path)
    audit = tmp_path / "audit.jsonl"
    with web_server(tmp_path, monkeypatch) as server:
        status, _, cookie = call(server, "POST", "/api/run",
                                 {"skill": str(skill), "tier": "t1"})
        assert status == 200
        web.ACTIVE.clear()
        for _ in range(2):                      # 游客 T1 默认 3 次/小时
            assert call(server, "POST", "/api/run",
                        {"skill": str(skill), "tier": "t1"}, cookie=cookie)[0] == 200
            web.ACTIVE.clear()
        assert usage_rows(tmp_path) == 3

        before = {p.name for p in (tmp_path / "runs").iterdir()}
        status, data, headers, _ = request(server, "POST", "/api/run",
                                          {"skill": str(skill), "tier": "t1"}, cookie=cookie)
        assert status == 429
        assert data["reason"] == "hourly" and data["scope"] == "actor"
        assert data["retry_after"] > 0 and int(headers["retry-after"]) == data["retry_after"]
        assert "配额" in data["error"]
        assert {p.name for p in (tmp_path / "runs").iterdir()} == before   # 不建 run 目录
        assert usage_rows(tmp_path) == 3                                   # 拒绝不消耗额度

        limited = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()
                   if json.loads(line)["action"] == "rate_limited"]
        assert limited and limited[-1]["status"] == 429
        assert limited[-1]["tier"] == "t1" and limited[-1]["actor_kind"] == "guest"
        assert "hourly" in limited[-1]["detail"]


def test_guest_t1_exhausted_still_allows_t0(tmp_path, monkeypatch):
    skill = make_skill(tmp_path)
    with web_server(tmp_path, monkeypatch) as server:
        _, _, cookie = call(server, "POST", "/api/run", {"skill": str(skill), "tier": "t1"})
        web.ACTIVE.clear()
        for _ in range(2):
            call(server, "POST", "/api/run", {"skill": str(skill), "tier": "t1"}, cookie=cookie)
            web.ACTIVE.clear()
        assert call(server, "POST", "/api/run", {"skill": str(skill), "tier": "t1"},
                    cookie=cookie)[0] == 429
        # 游客 T1 与 T0 是两个额度桶：T1 用完仍能跑基础检查
        status, data, _ = call(server, "POST", "/api/run",
                               {"skill": str(skill), "tier": "t0"}, cookie=cookie)
        assert status == 200, data


def test_token_quota_is_not_shared_by_ip(tmp_path, monkeypatch):
    """token 有明确身份，不能被同一 NAT 的其他 token 连坐。"""
    monkeypatch.setattr(web.ratelimit, "DB", tmp_path / "usage.sqlite3")
    monkeypatch.setattr(web.ratelimit, "LIMITS_FILE", tmp_path / "limits.json")
    a = {"kind": "token", "id": "tok_a", "name": "A"}
    b = {"kind": "token", "id": "tok_b", "name": "B"}
    for _ in range(3):
        assert web.ratelimit.record(a, "t2a", ip="203.0.113.9")
    result = web.ratelimit.check(b, "t2a", ip="203.0.113.9")
    assert result["allowed"] is True, result
    denied = web.ratelimit.check(a, "t2a", ip="203.0.113.9")
    assert denied["allowed"] is False and denied["scope"] == "actor"

def test_guest_cookie_rotation_cannot_bypass_ip_limit(tmp_path, monkeypatch):
    skill = make_skill(tmp_path)
    with web_server(tmp_path, monkeypatch) as server:
        _, _, cookie = call(server, "POST", "/api/run", {"skill": str(skill), "tier": "t1"})
        web.ACTIVE.clear()
        for _ in range(2):
            call(server, "POST", "/api/run", {"skill": str(skill), "tier": "t1"}, cookie=cookie)
            web.ACTIVE.clear()
        # 换一个游客 cookie（新 id）仍被 IP 桶挡住：IP 只辅助，但换 cookie 不是绕过手段
        status, data, _ = call(server, "POST", "/api/run",
                               {"skill": str(skill), "tier": "t1"},
                               cookie="se_guest=" + "c" * 32)
        assert status == 429 and data["scope"] == "ip" and data["reason"] == "hourly"


def test_token_quota_override_from_record(tmp_path, monkeypatch):
    skill = make_skill(tmp_path)
    tokens = tmp_path / "tokens.json"
    with web_server(tmp_path, monkeypatch) as server:
        token = web.token_admin.issue("限额用户")
        patch_token_quota(tokens, token["id"], daily_runs=2)
        headers = {"X-Skill-Eval-Token": token["token"]}
        for _ in range(2):
            status, data, _ = call(server, "POST", "/api/run",
                                   {"skill": str(skill), "tier": "t2a"}, headers=headers)
            assert status == 200, data
            web.ACTIVE.clear()
        status, data, _ = call(server, "POST", "/api/run",
                               {"skill": str(skill), "tier": "t2a"}, headers=headers)
        assert status == 429 and data["reason"] == "daily"

        # 额度按 token 独立：没带覆盖字段的另一个 token 仍按默认 8 次/天
        other = web.token_admin.issue("普通用户")
        status, data, _ = call(server, "POST", "/api/run", {"skill": str(skill), "tier": "t2a"},
                               headers={"X-Skill-Eval-Token": other["token"]})
        assert status == 200, data


def test_token_concurrency_override_and_per_actor_counting(tmp_path, monkeypatch):
    skill = make_skill(tmp_path)
    tokens = tmp_path / "tokens.json"
    with web_server(tmp_path, monkeypatch) as server:
        token = web.token_admin.issue("单并发用户")
        patch_token_quota(tokens, token["id"], max_concurrent=1)
        headers = {"X-Skill-Eval-Token": token["token"]}
        assert call(server, "POST", "/api/run",
                    {"skill": str(skill), "tier": "t2a"}, headers=headers)[0] == 200
        status, data, _ = call(server, "POST", "/api/run",
                               {"skill": str(skill), "tier": "t2a"}, headers=headers)
        assert status == 429 and data["reason"] == "concurrent" and data["scope"] == "actor"
        # 并发按身份算：另一个 token 还能起（全局默认 4 并发）
        other = web.token_admin.issue("另一个用户")
        status, data, _ = call(server, "POST", "/api/run", {"skill": str(skill), "tier": "t2a"},
                               headers={"X-Skill-Eval-Token": other["token"]})
        assert status == 200, data


def test_global_concurrency_blocks_other_actor(tmp_path, monkeypatch):
    skill = make_skill(tmp_path)
    with web_server(tmp_path, monkeypatch) as server:
        # 本地额度配置在 check 时读取：不重启服务即可生效
        (tmp_path / "limits.json").write_text(json.dumps({"global": {"concurrent": 1}}),
                                              encoding="utf-8")
        assert call(server, "POST", "/api/run", {"skill": str(skill), "tier": "t0"})[0] == 200
        status, data, _ = call(server, "POST", "/api/run",
                               {"skill": str(skill), "tier": "t0"},
                               cookie="se_guest=" + "d" * 32)
        assert status == 429 and data["scope"] == "global" and data["reason"] == "concurrent"


def test_cleanup_drops_records_older_than_retention(tmp_path, monkeypatch):
    monkeypatch.setattr(web.ratelimit, "DB", tmp_path / "usage.sqlite3")
    actor = {"kind": "guest", "id": "g" * 32, "name": "游客"}
    assert web.ratelimit.record(actor, "t0") is True
    conn = sqlite3.connect(str(tmp_path / "usage.sqlite3"))
    conn.execute("INSERT INTO usage (ts, bucket, actor_id, ip, action) VALUES (?, ?, ?, ?, ?)",
                 (time.time() - 40 * 86400, "guest.t0", "old", "127.0.0.1", "run"))
    conn.commit()
    conn.close()
    assert web.ratelimit.cleanup() == 1
    assert usage_rows(tmp_path) == 1


def test_ui_shows_quota_error_for_429():
    html = (WEB_APP.parent / "index.html").read_text(encoding="utf-8")
    assert 'r.status === 429' in html
    assert "配额已用完" in html and "retry_after" in html
    assert 'id="errorBar"' in html          # 沿用现有错误条，不另起一套提示


def test_quota_survives_server_restart(tmp_path, monkeypatch):
    skill = make_skill(tmp_path)
    with web_server(tmp_path, monkeypatch) as server:
        _, _, cookie = call(server, "POST", "/api/run", {"skill": str(skill), "tier": "t1"})
        web.ACTIVE.clear()
        for _ in range(2):
            call(server, "POST", "/api/run", {"skill": str(skill), "tier": "t1"}, cookie=cookie)
            web.ACTIVE.clear()
    # 重启等价：ACTIVE 清空、计数库留在磁盘上，额度不因重启复位
    with web_server(tmp_path, monkeypatch) as server:
        status, data, _ = call(server, "POST", "/api/run",
                               {"skill": str(skill), "tier": "t1"}, cookie=cookie)
        assert status == 429 and data["reason"] == "hourly"


# --- 容器能力：/api/capabilities、T2/T3 412、T0/T1 不受影响 ---

def deny_container_capability(tier, *, adapter=None, isolation=None, sandbox_status=None):
    if tier in web.capabilities.CONTAINER_TIERS:
        return {"tier": tier, "isolation": "docker", "ok": False,
                "missing": ["docker"], "reasons": ["缺少 docker CLI：需要容器运行时"],
                "fix": ["安装 Docker Desktop"], "warnings": [], "container": None,
                "adapter": {"name": adapter or "pi", "available": True, "reason": ""}}
    return allow_capability(tier, adapter=adapter, isolation=isolation)


def test_capabilities_endpoint_lists_every_tier(tmp_path, monkeypatch):
    with web_server(tmp_path, monkeypatch) as server:
        status, data, _ = call(server, "GET", "/api/capabilities")
    assert status == 200
    assert set(data["tiers"]) == {"t0", "t1", "t2a", "t2b", "t3"}
    assert data["tiers"]["t0"]["ok"] is True
    assert data["container"]["engine"] == "docker"
    assert data["model_proxy"]["configured"] is False


def test_t2_without_container_returns_412_without_side_effects(tmp_path, monkeypatch):
    skill = make_skill(tmp_path)
    audit = tmp_path / "audit.jsonl"
    with web_server(tmp_path, monkeypatch) as server:
        monkeypatch.setattr(web.capabilities, "check", deny_container_capability)
        token = web.token_admin.issue("容器缺失用户")
        before = {p.name for p in (tmp_path / "runs").iterdir()}
        status, data, headers, _ = request(
            server, "POST", "/api/run", {"skill": str(skill), "tier": "t2a"},
            headers={"X-Skill-Eval-Token": token["token"]})
        assert status == 412
        assert data["missing"] == ["docker"] and data["isolation"] == "docker"
        assert "容器运行时" in data["error"] and data["fix"]
        assert {p.name for p in (tmp_path / "runs").iterdir()} == before   # 不建 run 目录
        assert not (tmp_path / "usage.sqlite3").exists()                   # 不扣配额
        events = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        missing = [e for e in events if e["action"] == "dep_missing"]
        assert missing and missing[-1]["status"] == 412 and missing[-1]["tier"] == "t2a"
        assert "docker" in missing[-1]["detail"]


def test_t0_t1_still_run_when_container_missing(tmp_path, monkeypatch):
    skill = make_skill(tmp_path)
    with web_server(tmp_path, monkeypatch) as server:
        monkeypatch.setattr(web.capabilities, "check", deny_container_capability)
        for tier in ("t0", "t1"):
            status, data, _ = call(server, "POST", "/api/run",
                                   {"skill": str(skill), "tier": tier})
            assert status == 200, data
            web.ACTIVE.clear()
        token = web.token_admin.issue("容器缺失用户")
        status, data, _ = call(server, "POST", "/api/run",
                               {"skill": str(skill), "tier": "t2a"},
                               headers={"X-Skill-Eval-Token": token["token"]})
        assert status == 412


def test_t0_t1_do_not_pass_evalset_to_runner(tmp_path, monkeypatch):
    skill = make_skill(tmp_path)
    captured = {}

    class _CaptureProc(_FakeProc):
        def __init__(self, cmd=None, **kwargs):
            super().__init__(cmd, **kwargs)
            captured.setdefault("cmds", []).append(list(cmd or []))

    with web_server(tmp_path, monkeypatch) as server:
        monkeypatch.setattr(web.subprocess, "Popen", _CaptureProc)
        status, data, _ = call(server, "POST", "/api/run",
                               {"skill": str(skill), "tier": "t0",
                                "evalset": str(tmp_path / "missing-evalset")})
        assert status == 200, data
        cmd = captured["cmds"][-1]
        assert "--evalset" not in cmd


def test_container_run_passes_isolation_to_runner(tmp_path, monkeypatch):
    skill = make_skill(tmp_path)
    captured = {}

    class _CaptureProc(_FakeProc):
        def __init__(self, cmd=None, **kwargs):
            super().__init__(cmd, **kwargs)
            captured.setdefault("cmds", []).append(list(cmd or []))

    with web_server(tmp_path, monkeypatch) as server:
        monkeypatch.setattr(web.subprocess, "Popen", _CaptureProc)
        token = web.token_admin.issue("容器用户")
        status, data, _ = call(server, "POST", "/api/run",
                               {"skill": str(skill), "tier": "t3"},
                               headers={"X-Skill-Eval-Token": token["token"]})
        assert status == 200, data
        cmd = captured["cmds"][-1]
        assert "--isolation" in cmd and cmd[cmd.index("--isolation") + 1] == "docker"


def test_ui_evidence_provenance_permissions_history_and_mobile():
    html = (WEB_APP.parent / "index.html").read_text(encoding="utf-8")
    # 1 证据：表格化 + 原始 JSON 折叠
    assert "function renderEvidenceData" in html and "证据数据" in html
    assert "原始 JSON" in html and "evidence-table" in html
    # 2 溯源条包含评测集
    assert '["评测集", evalsetText]' in html
    # 3 403 不再伪装成 404
    assert "无权访问这次运行" in html and "r.status === 403" in html
    # 4 最近报告只显示有报告的 run
    assert ".filter(x=>x.has_report)" in html
    # 5 人工改判历史
    assert "renderOverrideHistory" in html and "改判历史" in html
    assert "manual_overrides" in html
    # 7 手机宽度：顶部换行 + token 移入弹层
    assert "flex-wrap:wrap" in html
    assert ".token-set{display:none}" in html.replace(" ", "")
    assert 'id="accessTokenMobile"' in html and 'id="tokenModal"' in html
    # 6 本机单用户模式
    assert 'fetch("/api/mode")' in html and "singleUser" in html
    assert 'id="modeBadge"' in html and "mode-single" in html


def test_rerun_metric_endpoint_checks_owner_and_calls_helper(tmp_path, monkeypatch):
    with web_server(tmp_path, monkeypatch) as server:
        token = web.token_admin.issue("重跑用户")
        headers = {"X-Skill-Eval-Token": token["token"]}
        rdir = write_owned_run(tmp_path / "runs", "rerunrun", owner_kind="token",
                               owner_id=token["id"], protected=False, tier="t1")
        (rdir / "results" / "judges").mkdir()
        for metric in ("brevity", "redundancy", "fallback", "precheck", "contract", "deps", "pii"):
            (rdir / "results" / "judges" / f"{metric}.json").write_text(
                json.dumps({"items": [], "score": 1.0}), encoding="utf-8")
        calls = []
        monkeypatch.setattr(web.rerun_metrics, "rerun_metric",
                            lambda results, metric, **kw: calls.append((str(results), metric, kw))
                            or {"ok": True, "metric": metric, "rubric": "brevity"})
        status, data, _ = call(server, "POST", "/api/rerun-metrics",
                               {"id": "rerunrun", "metric": "#2", "llm": {"model": "m"}},
                               headers=headers)
        assert status == 200 and data["ok"] is True
        assert calls and calls[0][1] == "#2"
        events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
        assert any(e["action"] == "rerun_metric" and e["run_id"] == "rerunrun" for e in events)
        other = web.token_admin.issue("别人")
        assert call(server, "POST", "/api/rerun-metrics",
                    {"id": "rerunrun", "metric": "#2"},
                    headers={"X-Skill-Eval-Token": other["token"]})[0] == 403


def test_ui_wires_single_metric_rerun():
    html = (WEB_APP.parent / "index.html").read_text(encoding="utf-8")
    assert "RERUN_METRICS" in html and 'class="btn btn-ghost btn-sm rerun-btn"' in html
    assert "/api/rerun-metrics" in html and "只重跑 ${metric}" in html


def test_ui_loads_capabilities_and_blocks_container_tiers():
    html = (WEB_APP.parent / "index.html").read_text(encoding="utf-8")
    assert 'fetch("/api/capabilities?adapter="' in html
    assert 'id="capabilityNote"' in html
    assert "adapterLabel" in html and "claude-code" in html
    assert "缺少容器运行时" in html
    assert "当前深度不可运行" in html
    assert "ADR" not in html and "T2B" not in html


# --- 无容器时的宿主回退：必须由用户确认（allow_unsafe_host === true）---

def fallback_capability(tier, *, adapter=None, isolation=None, sandbox_status=None):
    """缺容器但 adapter 可用的能力桩：docker 检查失败，fallback 指向宿主。"""
    if tier not in web.capabilities.CONTAINER_TIERS:
        return allow_capability(tier, adapter=adapter, isolation=isolation)
    adapter_info = {"name": adapter or "pi", "available": True, "reason": ""}
    fallback = {"isolation": "host", "available": True,
                "reason": "没有可用容器运行时：确认后可在宿主临时目录执行"}
    if (isolation or "docker") == "host":
        return {"tier": tier, "isolation": "host", "ok": True, "missing": [],
                "reasons": [], "fix": [], "warnings": ["宿主临时目录，不是安全边界"],
                "container": None, "adapter": adapter_info, "fallback": fallback}
    return {"tier": tier, "isolation": "docker", "ok": False, "missing": ["docker"],
            "reasons": ["缺少 docker CLI：需要容器运行时"],
            "fix": ["安装 Docker Desktop"], "warnings": [], "container": None,
            "adapter": adapter_info, "fallback": fallback}


def test_unsafe_host_requires_boolean_true_flag(tmp_path, monkeypatch):
    skill = make_skill(tmp_path)
    with web_server(tmp_path, monkeypatch) as server:
        monkeypatch.setattr(web.capabilities, "check", fallback_capability)
        token = web.token_admin.issue("无容器用户")
        headers = {"X-Skill-Eval-Token": token["token"]}
        before = {p.name for p in (tmp_path / "runs").iterdir()}
        bodies = [
            {"skill": str(skill), "tier": "t2a"},                                  # 未确认
            {"skill": str(skill), "tier": "t2a", "allow_unsafe_host": "true"},     # 字符串不算
            {"skill": str(skill), "tier": "t2a", "allow_unsafe_host": 1},          # 非布尔
        ]
        for body in bodies:
            status, data, _ = call(server, "POST", "/api/run", body, headers=headers)
            assert status == 412, data
            assert data["isolation"] == "docker"
            assert data["fallback"]["available"] is True
            assert "容器运行时" in data["error"]
        assert {p.name for p in (tmp_path / "runs").iterdir()} == before   # 不建 run 目录
        assert not (tmp_path / "usage.sqlite3").exists()                   # 不扣配额


def test_confirmed_unsafe_host_runs_with_isolation_host(tmp_path, monkeypatch):
    skill = make_skill(tmp_path)
    captured = {}

    class _CaptureProc(_FakeProc):
        def __init__(self, cmd=None, **kwargs):
            super().__init__(cmd, **kwargs)
            captured.setdefault("cmds", []).append(list(cmd or []))

    with web_server(tmp_path, monkeypatch) as server:
        monkeypatch.setattr(web.capabilities, "check", fallback_capability)
        monkeypatch.setattr(web.subprocess, "Popen", _CaptureProc)
        token = web.token_admin.issue("无容器用户")
        status, data, _ = call(server, "POST", "/api/run",
                               {"skill": str(skill), "tier": "t2a",
                                "allow_unsafe_host": True},
                               headers={"X-Skill-Eval-Token": token["token"]})
        assert status == 200, data
        assert data["isolation"] == "host" and data["unsafe_host"] is True
        cmd = captured["cmds"][-1]
        assert "--isolation" in cmd and cmd[cmd.index("--isolation") + 1] == "host"
        run_json = json.loads((tmp_path / "runs" / data["id"] / "run.json")
                              .read_text(encoding="utf-8"))
        assert run_json["isolation"] == "host" and run_json["unsafe_host"] is True
        events = [json.loads(line) for line in
                  (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
        started = [item for item in events if item["action"] == "run_start"]
        assert started and started[-1]["detail"] == "unsafe_host"


def test_allow_unsafe_host_does_not_bypass_available_container(tmp_path, monkeypatch):
    skill = make_skill(tmp_path)
    captured = {}

    class _CaptureProc(_FakeProc):
        def __init__(self, cmd=None, **kwargs):
            super().__init__(cmd, **kwargs)
            captured.setdefault("cmds", []).append(list(cmd or []))

    with web_server(tmp_path, monkeypatch) as server:
        monkeypatch.setattr(web.subprocess, "Popen", _CaptureProc)
        token = web.token_admin.issue("容器用户")
        status, data, _ = call(server, "POST", "/api/run",
                               {"skill": str(skill), "tier": "t3",
                                "allow_unsafe_host": True},
                               headers={"X-Skill-Eval-Token": token["token"]})
        assert status == 200, data
        assert data["isolation"] == "docker" and data["unsafe_host"] is False
        cmd = captured["cmds"][-1]
        assert cmd[cmd.index("--isolation") + 1] == "docker"
        run_json = json.loads((tmp_path / "runs" / data["id"] / "run.json")
                              .read_text(encoding="utf-8"))
        assert run_json["isolation"] == "docker" and run_json["unsafe_host"] is False


def test_ui_confirms_before_unsafe_host_fallback():
    html = (WEB_APP.parent / "index.html").read_text(encoding="utf-8")
    assert "allow_unsafe_host: allowUnsafeHost" in html
    assert "fallbackInfo" in html
    assert "可回退宿主" in html and "完全不可运行" in html
    assert "没有可用的容器" in html
    assert "仍可能读到宿主文件" in html and "confirm(" in html
    # 未确认时不自动回退：只有 confirm 通过后才置 allowUnsafeHost
    assert html.index("confirm(\"当前环境没有可用的容器") < html.index("allowUnsafeHost = true")


# --- 成本预估：GET /api/estimate（只读、不建 run、不扣配额、不需要 token）---

def test_estimate_endpoint_counts_without_side_effects(tmp_path, monkeypatch):
    skill = make_skill(tmp_path)
    evalsets = tmp_path / "evalsets"
    monkeypatch.setattr(web, "EVALSETS", evalsets)
    evalset = make_evalset(evalsets, "demo", "v1")
    query = (f"/api/estimate?skill={quote(str(skill))}&tier=t2a"
             f"&evalset={quote(str(evalset))}")
    with web_server(tmp_path, monkeypatch) as server:
        status, data, _ = call(server, "GET", query)
        assert status == 200, data
        assert set(data) >= {"calls", "agent_runs", "tokens", "minutes",
                             "stages", "weights", "cached", "assumptions"}
        # 固定评测集：1 条 should 触发 → 1 个 agent，判定 0–1 次，另加 T1 的 7 次
        assert data["agent_runs"] == 1
        assert data["calls"]["max"] == 8
        assert data["cached"] == []
        assert not list((tmp_path / "runs").iterdir())      # 不建 run 目录
        assert not (tmp_path / "usage.sqlite3").exists()    # 不扣配额


def test_estimate_endpoint_rejects_invalid_inputs(tmp_path, monkeypatch):
    skill = make_skill(tmp_path)
    outside = tmp_path / "outside" / "v1"
    (outside / "triggers" / "should").mkdir(parents=True)
    (outside / "triggers" / "should" / "s1.json").write_text(
        json.dumps({"prompt": "x"}), encoding="utf-8")
    (tmp_path / "evalsets").mkdir()
    monkeypatch.setattr(web, "EVALSETS", tmp_path / "evalsets")
    with web_server(tmp_path, monkeypatch) as server:
        status, data, _ = call(server, "GET",
                               f"/api/estimate?skill={quote(str(skill))}&tier=t2a"
                               f"&evalset={quote(str(outside))}")
        assert status == 400 and "evalsets" in data["error"]
        assert call(server, "GET", "/api/estimate?tier=t0")[0] == 400
        assert call(server, "GET",
                    f"/api/estimate?skill={quote(str(skill))}&tier=t9")[0] == 400
        assert call(server, "GET",
                    f"/api/estimate?skill={quote(str(tmp_path / 'nope'))}&tier=t0")[0] == 400
        assert not (tmp_path / "usage.sqlite3").exists()


def test_estimate_endpoint_reports_cached_stages(tmp_path, monkeypatch):
    skill = make_skill(tmp_path)
    with web_server(tmp_path, monkeypatch) as server:
        results = tmp_path / "runs" / "prev" / "results"
        results.mkdir(parents=True)
        (results / "run-meta.json").write_text(json.dumps({
            "skill": str(skill), "created_at": "2026-01-02T00:00:00"}), encoding="utf-8")
        (results / "static.json").write_text(json.dumps({"passed": True}), encoding="utf-8")
        status, data, _ = call(server, "GET",
                               f"/api/estimate?skill={quote(str(skill))}&tier=t1")
        assert status == 200, data
        assert data["cached"] == ["T0"]
        assert data["calls"] == {"min": 7, "max": 7}   # T1 未缓存


def test_ui_wires_estimate_form_memory_and_dynamic_weights():
    html = (WEB_APP.parent / "index.html").read_text(encoding="utf-8")
    # 1 成本条：所有档位都显示，输入变化防抖刷新
    assert 'id="estimateBar"' in html and 'id="estimateMain"' in html
    assert 'fetch("/api/estimate?' in html and "scheduleEstimate" in html
    assert "estimateSeq" in html            # 过期请求不覆盖新结果
    # 2 动态权重：estimate 的 weights 进入 stageBounds，plan 无 weights 时不覆盖
    assert "function setPlan(tier, stages, weights)" in html
    assert "setPlan(ev.tier, ev.stages, ev.weights)" in html
    assert "function stageWeight(name)" in html and "stageWeight(item)" in html
    assert "if(weights && typeof weights === \"object\") planWeights" in html
    # 3 计时用 estimate 的 minutes，超时文案不暗示卡死
    assert "function estimateMinutes()" in html and "currentEstimate.minutes" in html
    assert "这次比预估慢，可能原因：模型排队、长 case、容器启动或重试；仍在运行" in html
    assert "已超过预估" not in html
    # 4 配置记忆：se-form 存选择、恢复在 workspaces 之后、API key 不落盘
    assert 'const FORM_KEY = "se-form"' in html
    assert "localStorage.setItem(FORM_KEY" in html
    assert "async function loadWorkspacesThenRestore" in html
    assert html.index("await loadWorkspaces()") < html.index("restoreForm();")
    start = html.index("function formState(")
    form_block = html[start:html.index("function saveForm", start)]
    assert "api_key" not in form_block and "token" not in form_block.lower()
    assert "formTouched" in html              # 用户先手动改过就不覆盖


# --- Phase B：报告当文档（锚点 / 筛选 / 统一弹窗 / 下一步 / 手机卡片）---

def test_ui_wires_report_metric_anchors_and_filter():
    html = (WEB_APP.parent / "index.html").read_text(encoding="utf-8")
    # 1 指标行锚点：id=m-N 与 report.html 的 #m-N 兼容；深链打开后滚动并高亮
    assert "function metricId(metric)" in html
    assert 'id="${metricId(k)}" data-metric="${esc(k)}" data-verdict="${esc(v.verdict)}"' in html
    assert "function scrollToMetric(metric)" in html and "anchor-hit" in html
    assert "function applyHashAnchor()" in html and "location.hash" in html
    assert "requestAnimationFrame(()=>applyHashAnchor())" in html
    assert r"^#m-\d+$" in html
    assert 'if(el.hidden) applyMetricFilter("all")' in html   # 目标被筛掉时先显示再定位
    # 2 筛选：四个按钮，只改可见行，组标题无可显示行时隐藏
    for key in ("all", "fail", "warn", "skipped"):
        assert f'data-filter="{key}"' in html, key
    assert "function applyMetricFilter(filter)" in html
    assert "row.hidden = !show" in html and "group.hidden = !any" in html
    assert "只改可见行，不重算指标" in html
    assert 'id="filterCount"' in html and "显示 ${shown}/${rows.length} 条" in html


def test_ui_wires_unified_modal_focus_and_escape():
    html = (WEB_APP.parent / "index.html").read_text(encoding="utf-8")
    assert "const modalStack = []" in html
    assert "function showModal(id)" in html and "function hideModal(id)" in html
    assert 'e.key === "Escape"' in html and 'e.key !== "Tab"' in html
    assert "document.activeElement === first" in html
    assert "document.activeElement === last" in html
    assert 'modal.setAttribute("role", "dialog")' in html
    assert 'modal.setAttribute("aria-modal", "true")' in html
    assert "modal._returnFocus" in html and "back.focus" in html
    modals = ("logModal", "tokenModal", "overridesModal", "evidenceModal",
              "historyModal", "compareModal", "adviceModal")
    for modal in modals:
        assert f'id="{modal}" role="dialog" aria-modal="true"' in html, modal
        assert f'showModal("{modal}")' in html, modal
        assert f'hideModal("{modal}")' in html, modal
        assert f'$("{modal}").hidden = true' not in html, modal
        assert f'$("{modal}").hidden = false' not in html, modal


def test_ui_wires_next_steps_and_advice_modal():
    html = (WEB_APP.parent / "index.html").read_text(encoding="utf-8")
    assert 'id="nextCard"' in html and 'id="nextList"' in html
    assert 'id="openAdvice"' in html and 'id="adviceModal"' in html
    assert "function renderNextSteps()" in html
    assert "function adviceSorted()" in html and "function advicePriority(item)" in html
    assert "ADVICE_EFFORT_RANK" in html and "ADVICE_EFFORT_LABELS" in html
    assert "advice.slice(0, 3)" in html
    assert "失败 ${counts.fail} · 警告 ${counts.warn} · 未测 ${counts.skipped}" in html
    assert 'href="#${metricId(item.metric)}"' in html and 'data-metric="${esc(item.metric)}"' in html
    assert "function renderAdvice()" in html
    for column in ("严重度", "问题", "证据", "建议动作", "工作量"):
        assert f"<th>{column}</th>" in html, column
    assert "renderNextSteps();" in html and "renderAdvice(); showModal(\"adviceModal\")" in html


def test_ui_report_table_becomes_cards_on_mobile():
    html = (WEB_APP.parent / "index.html").read_text(encoding="utf-8")
    mobile = html[html.rindex("@media(max-width:640px)"):]
    assert "#metrics{display:block;min-width:0;width:100%}" in mobile
    assert "#metrics thead{display:none}" in mobile
    assert "#metrics tr.metric-row{display:block" in mobile
    assert "td::before{content:attr(data-label)" in mobile
    assert "#metrics tr[hidden]{display:none}" in html
    for label in ("指标", "结果", "摘要", "证据"):
        assert f'data-label="{label}"' in html, label


# --- Phase C：评测集审核体验（质量面板 / 默认未审 / 过滤 / 增量合并）---

def test_evalset_review_map_handles_missing_corrupt_and_empty_verdict(tmp_path):
    ev = make_evalset(tmp_path / "evalsets", "demo", "v1")
    assert web._evalset_review_map(ev) == {}

    (ev / "reviews").mkdir()
    (ev / "reviews" / "review.json").write_text("{bad json", encoding="utf-8")
    assert web._evalset_review_map(ev) == {}

    (ev / "reviews" / "review.json").write_text(json.dumps({
        "reviewer": "张三", "reviewed_at": "2026-01-01T00:00:00",
        "items": [{"file": "triggers/should/s1.json", "verdict": "approve", "note": "好"},
                  {"file": "cases/c1.json", "verdict": ""}]}), encoding="utf-8")
    assert web._evalset_review_map(ev) == {
        "triggers/should/s1.json": {"verdict": "approve", "note": "好",
                                    "reviewer": "张三", "at": "2026-01-01T00:00:00"}}


def _review(server, evalset, decisions, headers, reviewer="张三"):
    return call(server, "POST", "/api/evalset/review",
                {"evalset": str(evalset), "decisions": decisions, "reviewer": reviewer},
                headers=headers)


def test_evalset_items_carry_review_and_review_merges_incrementally(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "EVALSETS", tmp_path / "evalsets")
    ev = make_evalset(tmp_path / "evalsets", "demo", "v1")
    (ev / "triggers" / "should" / "s2.json").write_text(
        json.dumps({"prompt": "第二个"}), encoding="utf-8")
    url = "/api/evalset/items?evalset=" + quote(str(ev))
    with web_server(tmp_path, monkeypatch) as server:
        token = web.token_admin.issue("张三")
        headers = {"X-Skill-Eval-Token": token["token"]}

        status, data, _ = call(server, "GET", url, headers=headers)
        assert status == 200, data
        assert [item["review"] for item in data["items"]] == [None, None, None]

        # 首次提交：只有 s1 有 verdict；s2 的空 verdict 不落盘
        status, data, _ = _review(server, ev, [
            {"file": "triggers/should/s1.json", "group": "should", "prompt": "帮我评估",
             "verdict": "approve", "note": "好"},
            {"file": "triggers/should/s2.json", "group": "should", "prompt": "第二个",
             "verdict": "", "note": "没选"}], headers)
        assert status == 200, data
        assert data["ok"] is True and data["reviewed"] == 1 and data["total"] == 3
        assert data["written"] is True
        saved = json.loads((ev / "reviews" / "review.json").read_text(encoding="utf-8"))
        assert [item["file"] for item in saved["items"]] == ["triggers/should/s1.json"]
        assert saved["items"][0]["reviewer"] == "张三" and saved["items"][0]["at"]

        # 再次提交：按 file 合并，s1 保留，s2 新增
        status, data, _ = _review(server, ev, [
            {"file": "triggers/should/s2.json", "group": "should", "prompt": "第二个",
             "verdict": "reject", "note": "太泛"}], headers)
        assert status == 200, data
        assert data["reviewed"] == 2 and data["total"] == 3
        saved = json.loads((ev / "reviews" / "review.json").read_text(encoding="utf-8"))
        by_file = {item["file"]: item for item in saved["items"]}
        assert set(by_file) == {"triggers/should/s1.json", "triggers/should/s2.json"}
        assert by_file["triggers/should/s1.json"]["note"] == "好"      # 已有记录不丢
        assert by_file["triggers/should/s2.json"]["verdict"] == "reject"

        # 刷新恢复：items 带回 review
        status, data, _ = call(server, "GET", url, headers=headers)
        assert status == 200
        review = {item["file"]: item["review"] for item in data["items"]}
        assert review["triggers/should/s1.json"]["verdict"] == "approve"
        assert review["triggers/should/s1.json"]["note"] == "好"
        assert review["triggers/should/s1.json"]["reviewer"] == "张三"
        assert review["triggers/should/s2.json"]["verdict"] == "reject"
        assert review["cases/c1.json"] is None

        # 只有空 verdict：不写盘、不升级 meta
        before = (ev / "reviews" / "review.json").read_bytes()
        meta_before = (ev / "meta.json").read_bytes()
        status, data, _ = _review(server, ev, [
            {"file": "cases/c1.json", "verdict": ""}], headers)
        assert status == 200 and data["reviewed"] == 2 and data["written"] is False
        assert (ev / "reviews" / "review.json").read_bytes() == before
        assert (ev / "meta.json").read_bytes() == meta_before


def test_evalset_review_upgrades_meta_only_when_every_item_approved(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "EVALSETS", tmp_path / "evalsets")
    ev = make_evalset(tmp_path / "evalsets", "demo", "v1")
    with web_server(tmp_path, monkeypatch) as server:
        token = web.token_admin.issue("张三")
        headers = {"X-Skill-Eval-Token": token["token"]}

        status, data, _ = _review(server, ev, [
            {"file": "triggers/should/s1.json", "verdict": "approve"}], headers)
        assert status == 200, data
        meta = json.loads((ev / "meta.json").read_text(encoding="utf-8"))
        assert "reviewed_by" not in meta          # 2 条里只审 1 条，不算人工复核完成

        status, data, _ = _review(server, ev, [
            {"file": "cases/c1.json", "verdict": "approve"}], headers)
        assert status == 200 and data["reviewed"] == 2
        meta = json.loads((ev / "meta.json").read_text(encoding="utf-8"))
        assert meta["reviewed_by"] == "human" and meta["reviewed_at"]


def test_ui_wires_evalset_quality_panel_filters_and_incremental_review():
    html = (WEB_APP.parent / "index.html").read_text(encoding="utf-8")
    # 1 质量面板：加载版本时调 status，展示条数闸门 + 自检问题与文件/覆盖率，可折叠可滚动
    assert 'id="evalQuality"' in html and "function renderEvalQuality" in html
    assert "/api/evalset/status?evalset=" in html and "loadEvalQuality" in html
    assert "evalset_check.py" in html
    for key in ("echoes_description", "echoes_name", "duplicates", "conflicts",
                "out_of_sandbox"):
        assert key in html, key
    assert "覆盖率 ${item.coverage}" in html
    assert "<details><summary>" in html and "qlist" in html and "max-height:180px" in html
    assert "不替代人工判断" in html
    # 2 默认未审，并从 review.json 恢复
    assert 'it.review' in html
    assert '<option value=""${verdict ? "" : " selected"}>未审</option>' in html
    assert '<select data-i="${i}" class="rv"><option value="approve">' not in html  # 不再默认通过
    # 3 搜索 / 组过滤 / 状态过滤 / 可见行全选 / 已审计数
    for element in ("evalSearch", "evalGroupFilter", "evalStatusFilter", "evalSelectAll",
                    "evalReviewedCount", "evalFilterCount", "evalBatchApprove",
                    "evalBatchReject"):
        assert f'id="{element}"' in html, element
    assert "function applyEvalFilter" in html and 'status === "pending"' in html
    assert "全选可见行" in html and "row.hidden" in html
    assert "已审 ${reviewed}/${evalItems.length}" in html
    # 4 批量通过二次确认，并把质量自检摘要带进提示
    assert "confirm(`把所选 ${rows.length} 条标为通过？" in html
    assert "质量自检：${evalQualitySummary" in html
    # 5 提交只发有 verdict 的行；成功后刷新 items + status
    assert ".filter(d=>d.verdict)" in html
    assert "没有已审的行" in html
    assert "await loadEvalData(evalset)" in html
