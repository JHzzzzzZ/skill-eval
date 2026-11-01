"""前端入口：stdlib HTTP 服务 + 原生 JS，只绑 127.0.0.1。

用法：python web/app.py [--port 8765]
页面选档位/执行载体/模型/评测集/真实输入 → 后台跑 scripts/runner.py → SSE 推进度。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

WEB = Path(__file__).resolve().parent
ROOT = WEB.parent
RUNNER = ROOT / "scripts" / "runner.py"
sys.path.insert(0, str(ROOT / "scripts"))
import llm as llm_layer  # noqa: E402
import runner as runner_mod  # noqa: E402
import static_check  # noqa: E402
RUNS = WEB / "runs"
RUNS.mkdir(parents=True, exist_ok=True)
WORKSPACES = WEB / "workspaces"
WORKSPACES.mkdir(parents=True, exist_ok=True)
ACTIVE = {}
MAX_UPLOAD_BYTES = 1024 ** 3
MAX_EXTRACTED_BYTES = 2 * 1024 ** 3
MAX_UPLOAD_FILES = 20000
MAX_EVALSET_BYTES = 50 * 1024 ** 2
MAX_EVALSET_FILES = 500
EVALSETS = ROOT.parent / "evalsets"


def _json_body(handler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0 or length > 2_000_000:
        return {}
    try:
        return json.loads(handler.rfile.read(length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _send_json(handler, code: int, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _send_bytes(handler, code: int, body: bytes, content_type: str, filename: str = ""):
    handler.send_response(code)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    if filename:
        handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    handler.end_headers()
    handler.wfile.write(body)


def _load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _safe_extract(zip_path: Path, dest: Path, *, max_zip_bytes: int = MAX_UPLOAD_BYTES,
                  max_bytes: int = MAX_EXTRACTED_BYTES,
                  max_files: int = MAX_UPLOAD_FILES) -> None:
    """解压 zip：拒绝绝对路径/.. /符号链接；限制文件数与总解压体积。"""
    if zip_path.stat().st_size > max_zip_bytes:
        raise ValueError("压缩包超过大小上限")
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        if len(infos) > max_files:
            raise ValueError(f"压缩包文件数超过 {max_files}")
        total = 0
        for info in infos:
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in Path(name).parts:
                raise ValueError(f"压缩包含非法路径：{name}")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError(f"压缩包含符号链接：{name}")
            total += info.file_size
            if total > max_bytes:
                raise ValueError("解压后体积超过上限")
        zf.extractall(dest)


def _receive_zip(handler, *, max_bytes: int = MAX_UPLOAD_BYTES,
                 subdir: str = "uploads") -> tuple[str, Path, Path]:
    """流式接收原始 zip（浏览器直接 POST File，不走 base64/JSON）。"""
    q = parse_qs(urlparse(handler.path).query)
    filename = (q.get("filename") or [""])[0]
    if not filename.lower().endswith(".zip"):
        raise ValueError("只支持 .zip")
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        raise ValueError("缺少 Content-Length")
    if length > max_bytes:
        handler.close_connection = True
        raise ValueError("压缩包超过大小上限")
    uid = uuid.uuid4().hex[:12]
    dest = RUNS / subdir / uid
    zpath = dest.with_suffix(".zip")
    zpath.parent.mkdir(parents=True, exist_ok=True)
    remaining = length
    try:
        with zpath.open("wb") as fh:
            while remaining > 0:
                chunk = handler.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("上传中断")
                fh.write(chunk)
                remaining -= len(chunk)
    except Exception:
        zpath.unlink(missing_ok=True)
        raise
    return uid, dest, zpath


def _skill_root(dest: Path) -> Path:
    if (dest / "SKILL.md").is_file():
        return dest
    found = [p.parent for p in dest.rglob("SKILL.md")
             if p.parent != dest and p.parent.parent == dest]
    if len(found) == 1:
        return found[0]
    raise ValueError("压缩包里找不到唯一的 SKILL.md（根目录或单一顶层目录）")


def _stop_process(proc) -> None:
    """终止 runner 及其子进程（Windows 用 taskkill /T，POSIX 用进程组）。"""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, text=True, timeout=10)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


def _stop_run(run) -> None:
    """停止一次运行：只杀进程并落 run.json 的 stopped 状态。

    stopped 事件由 /api/stop 在局部报告生成后统一追加一条，避免 SSE 先收到
    空 report 的事件就收尾。
    """
    _stop_process(run["proc"])
    run["stopped"] = True
    rdir = run.get("dir")
    if rdir:
        data = _run_json(rdir)
        data.update({"stopped": True, "stopped_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        try:
            (rdir / "run.json").write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
        except OSError:
            pass


def _slug(text: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", str(text or "")).strip("-._").lower()
    return value or "skill"


def _evalset_base(name: str, sha: str = "") -> Path:
    slug = _slug(name)
    if slug == "skill":
        # 非 ASCII 名称无法直接做目录名：用 skill_sha 或名称哈希，保证同一名称稳定同路径。
        digest = sha or hashlib.sha256(str(name or "").encode("utf-8")).hexdigest()
        slug = f"skill-{digest[:8]}"
    return EVALSETS / slug


def _evalset_versions(name: str, sha: str = "") -> list:
    base = _evalset_base(name, sha)
    out = []
    for p in sorted((x for x in base.glob("v*") if x.is_dir()),
                    key=lambda x: int(x.name[1:]) if x.name[1:].isdigit() else 0):
        meta = _load_json(p / "meta.json", {}) or {}
        recorded = str(meta.get("skill_sha256") or "")
        if not recorded:
            source = "unknown"
        elif sha and recorded == sha:
            source = "current"
        else:
            source = "old"
        out.append({"version": p.name, "path": str(p),
                    "frozen": bool(meta.get("frozen_at") or meta.get("reviewed_by")),
                    "reviewed_by": meta.get("reviewed_by"),
                    "skill_sha256": recorded or None,
                    "source": source})
    return out


def _evalset_for(name: str, sha: str = "") -> Path:
    versions = _evalset_versions(name, sha)
    return Path(versions[-1]["path"]) if versions else _evalset_base(name, sha) / "v1"


def _evalset_fields(name: str, sha: str) -> dict:
    versions = _evalset_versions(name, sha)
    current = versions[-1] if versions else None
    return {"evalset": current["path"] if current else str(_evalset_base(name, sha) / "v1"),
            "evalset_version": current["version"] if current else "v1",
            "evalset_versions": versions}


def _skill_name(skill: Path) -> str:
    try:
        meta, _ = static_check.parse_frontmatter(
            (skill / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
        return str(meta.get("name") or skill.name)
    except OSError:
        return skill.name


def _under_evalsets(path: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(EVALSETS.resolve())
    except (OSError, ValueError):
        return False


def _next_version(base: Path) -> str:
    nums = [int(p.name[1:]) for p in base.glob("v*") if p.is_dir() and p.name[1:].isdigit()]
    return f"v{max(nums) + 1 if nums else 1}"


def _evalset_export_zip(evalset: Path) -> tuple[str, bytes]:
    """把评测集打成 zip（根含 meta.json/triggers/cases/reviews，排除 results）。"""
    evalset = Path(evalset)
    if not evalset.is_dir():
        raise ValueError("评测集目录不存在")
    if not _under_evalsets(evalset):
        raise ValueError("只允许导出 evalsets/ 下的评测集")
    name, version = evalset.parent.name, evalset.name
    meta = _load_json(evalset / "meta.json", None)
    if not isinstance(meta, dict):
        meta = {"name": name, "version": version, "note": "meta.json 由导出时补全（原目录没有）"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
        for sub in ("triggers", "cases", "reviews"):
            base = evalset / sub
            if not base.is_dir():
                continue
            for p in sorted(base.rglob("*")):
                if p.is_file() and "results" not in p.relative_to(evalset).parts:
                    zf.write(p, p.relative_to(evalset).as_posix())
    return f"evalset-{_slug(name)}-{version}.zip", buf.getvalue()


def _evalset_import_root(dest: Path) -> Path:
    """自动定位 zip 根：根目录或单一顶层目录下含 meta.json / triggers / cases。"""
    marks = ("meta.json", "triggers", "cases")
    if any((dest / m).exists() for m in marks):
        return dest
    found = [p for p in dest.iterdir()
             if p.is_dir() and any((p / m).exists() for m in marks)]
    if len(found) == 1:
        return found[0]
    raise ValueError("压缩包里找不到评测集根目录（meta.json / triggers / cases）")


def _evalset_has_items(root: Path) -> bool:
    for sub in ("should", "should-not", "confusable"):
        if any((root / "triggers" / sub).glob("*.json")):
            return True
    return any((root / "cases").glob("*.json"))


def _evalset_name_from_filename(filename: str) -> str:
    stem = Path(filename or "").stem
    stem = re.sub(r"^evalset-", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"-v\d+$", "", stem, flags=re.IGNORECASE)
    return stem or "evalset"


def _import_evalset(root: Path, filename: str) -> dict:
    """把已解压的评测集复制进 evalsets/<name>/vN；目标已存在则自动下一个版本，绝不覆盖。"""
    if not _evalset_has_items(root):
        raise ValueError("评测集至少需要一个触发组或 cases")
    meta = _load_json(root / "meta.json", None)
    meta = dict(meta) if isinstance(meta, dict) else {}
    name = str(meta.get("name") or _evalset_name_from_filename(filename)).strip() \
        or _evalset_name_from_filename(filename)
    version = str(meta.get("version") or "v1")
    if not re.fullmatch(r"v\d+", version):
        version = "v1"
    base = _evalset_base(name)
    target = base / version
    if target.exists():
        target = base / _next_version(base)
    target.mkdir(parents=True, exist_ok=False)
    for sub in ("triggers", "cases", "reviews"):
        if (root / sub).is_dir():
            shutil.copytree(root / sub, target / sub,
                            ignore=shutil.ignore_patterns("results", "__pycache__"))
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    meta.update({"name": name, "version": target.name, "imported_at": now,
                 "source_archive": Path(filename or "").name})
    meta.setdefault("created_at", now)
    (target / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    return {"ok": True, "name": name, "evalset": str(target),
            "evalset_version": target.name,
            **_evalset_fields(name, str(meta.get("skill_sha256") or ""))}


def _stamp_evalset_meta(evalset: Path, skill: Path, name: str) -> None:
    """新生成的评测集记录来源 skill 名称/哈希，供版本下拉区分“当前版本/基于旧版本”。"""
    evalset = Path(evalset)
    if not evalset.is_dir():
        return
    try:
        meta = _load_json(evalset / "meta.json", None)
        meta = dict(meta) if isinstance(meta, dict) else {}
        meta.setdefault("name", name)
        meta.setdefault("version", evalset.name)
        meta["skill_name"] = name
        meta["skill_sha256"] = runner_mod._skill_digest(Path(skill))
        meta.setdefault("created_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
        (evalset / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
    except OSError:
        pass


def _run_json(rdir: Path) -> dict:
    data = _load_json(Path(rdir) / "run.json", None)
    return data if isinstance(data, dict) else {}


RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _run_dir(rid: str) -> Path | None:
    """run id 只允许字母数字/下划线/短横——拒绝 `../` 之类的路径穿越。"""
    if not RUN_ID_RE.fullmatch(str(rid or "")):
        return None
    return RUNS / rid


def _results_dir(rid: str) -> Path | None:
    run = ACTIVE.get(rid)
    if run:
        return run["results"]
    rdir = _run_dir(rid)
    return rdir / "results" if rdir else None


def _disk_status(rid: str) -> dict | None:
    """磁盘上的历史运行：重启后 ACTIVE 丢失时，status/report/log 仍可读。"""
    rdir = _run_dir(rid)
    if not rdir or not rdir.is_dir():
        return None
    results = rdir / "results"
    run_meta = _load_json(results / "run-meta.json", None)
    run_meta = run_meta if isinstance(run_meta, dict) else {}
    run_json = _run_json(rdir)
    report = _load_json(results / "report.json", None)
    report = report if isinstance(report, dict) else {}
    if not results.is_dir() and not run_json:
        return None
    skill = str(run_meta.get("skill") or "")
    return {"id": rid, "running": False, "returncode": None,
            "stopped": bool(run_json.get("stopped")),
            "partial": bool(report.get("partial")),
            "completed_stages": report.get("completed_stages") or [],
            "results": str(results),
            "report": str(results / "report.html") if (results / "report.html").is_file() else "",
            "tier": run_json.get("tier") or run_meta.get("tier"),
            "skill_name": (run_json.get("skill_name") or run_meta.get("skill_name")
                           or (Path(skill).name if skill else "")),
            "created_at": run_json.get("created_at") or run_meta.get("created_at")}


def _status_payload(rid: str, run: dict | None) -> dict:
    if run:
        results = run["results"]
        report = _load_json(results / "report.json", None)
        report = report if isinstance(report, dict) else {}
        run_json = _run_json(run.get("dir") or results.parent)
        return {"id": rid, "running": run["proc"].poll() is None,
                "returncode": run["proc"].poll(), "stopped": bool(run.get("stopped")),
                "partial": bool(report.get("partial")),
                "completed_stages": report.get("completed_stages") or [],
                "results": str(results),
                "report": str(results / "report.html") if (results / "report.html").is_file() else "",
                "log": str(run.get("dir", results.parent) / "run.log"),
                "skill_name": run_json.get("skill_name", ""),
                "created_at": run_json.get("created_at"),
                "tier": run_json.get("tier")}
    disk = _disk_status(rid)
    if disk is None:
        return {}
    rdir = _run_dir(rid)
    disk["log"] = str(rdir / "run.log") if rdir and (rdir / "run.log").is_file() else ""
    return disk


def _runs(limit: int = 20) -> list:
    entries = []
    for rdir in RUNS.iterdir():
        if not rdir.is_dir() or rdir.name in ("uploads", "eval-uploads"):
            continue
        # ACTIVE 的运行还在跑：磁盘上没有 report/stopped 状态，_disk_status 会
        # 误报 running=false；优先用进程态，同时保留 has_report/conclusion。
        run = ACTIVE.get(rdir.name)
        status = _status_payload(rdir.name, run) if run else _disk_status(rdir.name)
        if status is None:
            continue
        try:
            mtime = rdir.stat().st_mtime
        except OSError:
            mtime = 0.0
        report = _load_json(rdir / "results" / "report.json", None)
        report = report if isinstance(report, dict) else {}
        entries.append({**status, "mtime": mtime,
                        "conclusion": report.get("conclusion"),
                        "has_report": (rdir / "results" / "report.html").is_file()})
    entries.sort(key=lambda e: (str(e.get("created_at") or ""), e["mtime"]), reverse=True)
    for entry in entries:
        entry.pop("mtime", None)
    return entries[:max(1, int(limit))]


def _tail_log(rid: str, lines: int = 200) -> str:
    rdir = _run_dir(rid)
    path = rdir / "run.log" if rdir else None
    if path is None or not path.is_file():
        raise ValueError("日志不存在")
    text = path.read_text(encoding="utf-8", errors="replace")
    return "\n".join(text.splitlines()[-max(1, int(lines)):])



def _workspace_meta(ws: Path) -> dict:
    try:
        meta = json.loads((ws / "workspace.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        meta = {}
    meta["skill"] = str(ws / "skill")
    return meta


def _workspace_upsert(skill: Path, source: str = "") -> dict:
    sha = runner_mod._skill_digest(skill)
    meta, _ = static_check.parse_frontmatter((skill / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
    name = str(meta.get("name") or skill.name).strip() or skill.name
    slug = _slug(name)
    wid = f"{slug}-{sha[:8]}" if slug != "skill" else f"skill-{sha[:8]}"
    ws = WORKSPACES / wid
    if not (ws / "workspace.json").is_file() or not (ws / "skill" / "SKILL.md").is_file():
        ws.mkdir(parents=True, exist_ok=True)
        if (ws / "skill").exists():
            shutil.rmtree(ws / "skill")
        shutil.copytree(skill, ws / "skill")
        payload = {"id": wid, "name": name, "skill_sha256": sha,
                   "source": source, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "last_used_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        (ws / "workspace.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        payload = _workspace_meta(ws)
        payload["last_used_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        (ws / "workspace.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"workspace_id": wid, "workspace": name, "skill": str(ws / "skill"),
            "skill_sha256": sha, **_evalset_fields(name, sha)}


def _workspaces() -> list:
    out = []
    for ws in sorted(WORKSPACES.iterdir()):
        if ws.is_dir() and (ws / "workspace.json").is_file():
            meta = _workspace_meta(ws)
            meta.update(_evalset_fields(meta.get("name") or ws.name,
                                        str(meta.get("skill_sha256") or "")))
            out.append(meta)
    return out


def _evalset_status(evalset: Path, skill: Path) -> dict:
    if not evalset.is_dir():
        raise ValueError(f"评测集目录不存在：{evalset}")
    count = subprocess.run([sys.executable, str(ROOT / "scripts" / "evalset_count.py"), str(evalset)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
    check = subprocess.run([sys.executable, str(ROOT / "scripts" / "evalset_check.py"),
                            str(evalset), "--skill", str(skill)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
    def _load(text):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"error": text[:300]}
    meta = {}
    try:
        meta = json.loads((evalset / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {"path": str(evalset), "count": _load(count.stdout),
            "check": _load(check.stdout), "meta": meta,
            "frozen": bool(meta.get("frozen_at"))}


def _copy_reuse(prev_results: Path, results: Path) -> bool:
    """把上次的 T0/T1 产物复制到新 results；runner 会用指纹再校验一次。"""
    if not prev_results.is_dir() or prev_results.resolve() == results.resolve():
        return False
    results.mkdir(parents=True, exist_ok=True)
    copied = False
    for name in ("static.json", "run-meta.json"):
        src = prev_results / name
        if src.is_file():
            shutil.copy2(src, results / name)
            copied = True
    for rel in ("judges", "raw/llm-judge"):
        src = prev_results / rel
        if src.is_dir():
            shutil.copytree(src, results / rel, dirs_exist_ok=True)
            copied = True
    return copied


def _completed_stages(results: Path) -> list:
    """按产物判定阶段完成度（不看进度日志）；当前半截阶段不算。"""
    return [name for name in runner_mod.TIERS if runner_mod.stage_ready(name, Path(results))]


def _rerun_report(run, *, partial: bool = False,
                  completed_stages: list | None = None) -> dict:
    meta = {}
    try:
        meta = json.loads((run["results"] / "run-meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    tier = (completed_stages[-1] if partial and completed_stages
            else meta.get("tier", ""))
    if not partial:
        # 人工调整/审核刷新重出报告时，不能把局部报告“升级”成完整报告
        prev = _load_json(run["results"] / "report.json", None)
        if isinstance(prev, dict) and prev.get("partial"):
            partial = True
            completed_stages = prev.get("completed_stages") or []
            tier = completed_stages[-1] if completed_stages else meta.get("tier", "")
    cmd = [sys.executable, str(ROOT / "scripts" / "report.py"), str(run["results"]),
           "--skill", meta.get("skill", ""), "--tier", tier, "--html"]
    if meta.get("evalset"):
        cmd += ["--evalset", meta["evalset"]]
    if partial:
        cmd += ["--partial"]
        if completed_stages:
            cmd += ["--completed-stages", ",".join(completed_stages)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-800:])
    return json.loads((run["results"] / "report.json").read_text(encoding="utf-8"))


def _finalize_partial(run) -> dict:
    """停止后只用最高完整阶段出局部报告；连基础检查都没完成则不生成空报告。"""
    stages = _completed_stages(run["results"])
    if "t0" not in stages:
        return {"ok": False, "error": "基础检查尚未完成，未生成局部报告",
                "completed_stages": stages, "report": ""}
    try:
        report = _rerun_report(run, partial=True, completed_stages=stages)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "completed_stages": stages, "report": ""}
    return {"ok": True, "report": str(run["results"] / "report.html"), "partial": True,
            "completed_stages": stages, "conclusion": report.get("conclusion")}


def _refresh_reports_for_evalset(evalset: Path) -> None:
    """审核写盘后刷新同一评测集的报告，保证报告页与审核页同步。"""
    target = evalset.resolve()
    seen = set()
    candidates = [run["results"] for run in ACTIVE.values()]
    candidates += [p.parent for p in RUNS.glob("*/results/run-meta.json")]
    for results in candidates:
        if results in seen or not (results / "report.json").is_file():
            continue
        seen.add(results)
        try:
            meta = json.loads((results / "run-meta.json").read_text(encoding="utf-8"))
            if not meta.get("evalset") or Path(meta["evalset"]).resolve() != target:
                continue
            _rerun_report({"results": results})
        except Exception:
            continue


class Handler(BaseHTTPRequestHandler):
    server_version = "skill-evaluator-web/2.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            html = (WEB / "index.html").read_text(encoding="utf-8")
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/llm/config":
            return _send_json(self, 200, llm_layer.status())
        if path == "/api/workspaces":
            return _send_json(self, 200, {"workspaces": _workspaces()})
        if path == "/api/runs":
            q = parse_qs(urlparse(self.path).query)
            try:
                limit = int((q.get("limit") or ["20"])[0])
            except ValueError:
                limit = 20
            return _send_json(self, 200, {"runs": _runs(limit)})
        if path == "/api/log":
            q = parse_qs(urlparse(self.path).query)
            rid = (q.get("id") or [""])[0]
            try:
                return _send_json(self, 200, {"id": rid, "log": _tail_log(rid)})
            except ValueError as exc:
                return _send_json(self, 404, {"error": str(exc)})
        if path == "/api/evalset/export":
            q = parse_qs(urlparse(self.path).query)
            evalset = (q.get("evalset") or [""])[0]
            if not evalset:
                return _send_json(self, 400, {"error": "需要 evalset"})
            try:
                filename, blob = _evalset_export_zip(Path(evalset))
            except Exception as exc:
                return _send_json(self, 400, {"error": str(exc)})
            return _send_bytes(self, 200, blob, "application/zip", filename)
        if path == "/api/evalset/status":
            q = parse_qs(urlparse(self.path).query)
            evalset = (q.get("evalset") or [""])[0]
            skill = (q.get("skill") or [""])[0]
            if not evalset or not skill:
                return _send_json(self, 400, {"error": "需要 evalset 与 skill"})
            try:
                return _send_json(self, 200, _evalset_status(Path(evalset), Path(skill)))
            except Exception as exc:
                return _send_json(self, 400, {"error": str(exc)})
        if path == "/api/evalset/items":
            q = parse_qs(urlparse(self.path).query)
            evalset = (q.get("evalset") or [""])[0]
            if not evalset or not Path(evalset).is_dir():
                return _send_json(self, 400, {"error": "评测集目录不存在"})
            items = []
            for group, sub in (("should", "should"), ("should_not", "should-not"),
                               ("confusable", "confusable")):
                for f in sorted((Path(evalset) / "triggers" / sub).glob("*.json")):
                    try:
                        data = json.loads(f.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    if isinstance(data, dict) and isinstance(data.get("prompt"), str):
                        items.append({"group": group, "file": f"triggers/{sub}/{f.name}",
                                      "prompt": data["prompt"]})
            for f in sorted((Path(evalset) / "cases").glob("*.json")):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(data, dict) and isinstance(data.get("prompt"), str):
                    items.append({"group": "case", "file": f"cases/{f.name}",
                                  "prompt": data["prompt"], "expect": data.get("expect") or ""})
            return _send_json(self, 200, {"items": items})
        if path == "/api/events":
            q = parse_qs(urlparse(self.path).query)
            rid = (q.get("id") or [""])[0]
            return self._events(rid)
        if path == "/api/status":
            q = parse_qs(urlparse(self.path).query)
            rid = (q.get("id") or [""])[0]
            payload = _status_payload(rid, ACTIVE.get(rid))
            if not payload:
                return _send_json(self, 404, {"error": "run not found"})
            return _send_json(self, 200, payload)
        if path == "/api/report":
            q = parse_qs(urlparse(self.path).query)
            rid = (q.get("id") or [""])[0]
            results = _results_dir(rid)
            report = results / "report.json" if results else None
            if report is None or not report.is_file():
                return _send_json(self, 404, {"error": "report not ready"})
            return _send_json(self, 200, json.loads(report.read_text(encoding="utf-8")))
        if path == "/report":
            q = parse_qs(urlparse(self.path).query)
            rid = (q.get("id") or [""])[0]
            results = _results_dir(rid)
            html_path = results / "report.html" if results else None
            if html_path is None or not html_path.is_file():
                return _send_json(self, 404, {"error": "report not ready"})
            body = html_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return _send_json(self, 404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/stop":
            data = _json_body(self)
            rid = str(data.get("id") or "")
            run = ACTIVE.get(rid)
            if not run:
                return _send_json(self, 404, {"error": "run not found"})
            if run["proc"].poll() is None:
                _stop_run(run)
            if run.get("stopped"):
                result = _finalize_partial(run)
            else:
                # 进程已自然结束：不拿局部报告覆盖完整报告
                existing = run["results"] / "report.html"
                prev = _load_json(run["results"] / "report.json", None)
                prev = prev if isinstance(prev, dict) else {}
                result = {"ok": existing.is_file(),
                          "completed_stages": _completed_stages(run["results"]),
                          "partial": bool(prev.get("partial")),
                          "report": str(existing) if existing.is_file() else "",
                          "error": "" if existing.is_file() else "运行已结束但没有生成报告"}
            run["finalized"] = True
            run["report"] = result.get("report") or ""
            run["completed_stages"] = result.get("completed_stages") or []
            run["stop_error"] = result.get("error")
            if run.get("stopped") and not run.get("stop_event_written"):
                # 报告已落盘，stopped 事件只写这一条，供 SSE 直接收尾。
                event = {"type": "stopped", "ts": time.time(),
                         "clock": time.strftime("%H:%M:%S"),
                         "report": run["report"],
                         "partial": bool(result.get("partial")),
                         "completed_stages": run["completed_stages"],
                         "error": run["stop_error"] or "用户终止评估"}
                run["stop_event_written"] = True
                try:
                    with run["progress"].open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                except OSError:
                    pass
            return _send_json(self, 200, {"ok": True, "id": rid,
                                          "running": run["proc"].poll() is None,
                                          "stopped": bool(run.get("stopped")),
                                          **result})
        if path == "/api/evalset/import":
            try:
                uid, dest, zpath = _receive_zip(self, max_bytes=MAX_EVALSET_BYTES,
                                                subdir="eval-uploads")
                filename = (parse_qs(urlparse(self.path).query).get("filename") or [""])[0]
                try:
                    _safe_extract(zpath, dest, max_zip_bytes=MAX_EVALSET_BYTES,
                                  max_bytes=MAX_EVALSET_BYTES, max_files=MAX_EVALSET_FILES)
                    root = _evalset_import_root(dest)
                    info = _import_evalset(root, filename)
                    info["upload_id"] = uid
                finally:
                    shutil.rmtree(dest, ignore_errors=True)
                    zpath.unlink(missing_ok=True)
                return _send_json(self, 200, info)
            except Exception as exc:
                return _send_json(self, 400, {"error": str(exc)})
        if path == "/api/upload":
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype in ("application/zip", "application/octet-stream"):
                try:
                    uid, dest, zpath = _receive_zip(self)
                    try:
                        _safe_extract(zpath, dest)
                        skill = _skill_root(dest)
                        filename = (parse_qs(urlparse(self.path).query).get("filename") or [""])[0]
                        info = _workspace_upsert(skill, filename)
                        info["upload_id"] = uid
                        shutil.rmtree(dest, ignore_errors=True)
                    except Exception:
                        shutil.rmtree(dest, ignore_errors=True)
                        raise
                    finally:
                        zpath.unlink(missing_ok=True)
                    return _send_json(self, 200, info)
                except Exception as exc:
                    return _send_json(self, 400, {"error": str(exc)})
            # 兼容旧的 base64 JSON 小包上传
            data = _json_body(self)
            raw = str(data.get("data_base64") or "")
            if not raw:
                return _send_json(self, 400, {"error": "没有上传内容"})
            try:
                blob = base64.b64decode(raw, validate=True)  # static-check:ignore
            except Exception:
                return _send_json(self, 400, {"error": "base64 解码失败"})
            if len(blob) > MAX_UPLOAD_BYTES:
                return _send_json(self, 400, {"error": "压缩包超过 1GB"})
            uid = uuid.uuid4().hex[:12]
            dest = RUNS / "uploads" / uid
            zpath = dest.with_suffix(".zip")
            zpath.parent.mkdir(parents=True, exist_ok=True)
            zpath.write_bytes(blob)
            try:
                _safe_extract(zpath, dest)
                skill = _skill_root(dest)
                info = _workspace_upsert(skill)
                info["upload_id"] = uid
                shutil.rmtree(dest, ignore_errors=True)
            except Exception as exc:
                shutil.rmtree(dest, ignore_errors=True)
                return _send_json(self, 400, {"error": str(exc)})
            finally:
                zpath.unlink(missing_ok=True)
            return _send_json(self, 200, info)
        if path == "/api/evalset/generate":
            data = _json_body(self)
            skill = str(data.get("skill") or "")
            out = str(data.get("out") or "")
            if not skill or not out:
                return _send_json(self, 400, {"error": "需要 skill 与 out"})
            cmd = [sys.executable, str(ROOT / "scripts" / "evalset_gen.py"),
                   "--skill", skill, "--out", out, "--count", str(int(data.get("count") or 10)),
                   "--backend", str(data.get("backend") or "auto")]
            for key, flag in (("model", "--model"), ("base_url", "--base-url"),
                              ("api_key", "--api-key"), ("protocol", "--llm-protocol")):
                if data.get(key):
                    cmd += [flag, str(data[key])]
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0:
                return _send_json(self, 400, {"error": (r.stdout or r.stderr)[-800:]})
            try:
                payload = json.loads(r.stdout)
            except json.JSONDecodeError:
                return _send_json(self, 500, {"error": r.stdout[-800:]})
            if payload.get("ok"):
                _stamp_evalset_meta(Path(out), Path(skill), _skill_name(Path(skill)))
            return _send_json(self, 200, payload)
        if path == "/api/evalset/review":
            data = _json_body(self)
            evalset = Path(str(data.get("evalset") or ""))
            decisions = data.get("decisions") or []
            if not evalset.is_dir() or not isinstance(decisions, list):
                return _send_json(self, 400, {"error": "需要评测集与 decisions"})
            reviews = evalset / "reviews"
            reviews.mkdir(parents=True, exist_ok=True)
            items = []
            for d in decisions:
                if not isinstance(d, dict):
                    continue
                items.append({"group": d.get("group"), "file": d.get("file"),
                              "verdict": d.get("verdict"), "note": d.get("note") or d.get("reason") or "",
                              "prompt": d.get("prompt"), "expect": d.get("expect") or ""})
            payload = {"type": "evalset-review", "evalset": evalset.parent.name,
                       "version": evalset.name,
                       "reviewer": str(data.get("reviewer") or "human"),
                       "reviewed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                       "items": items}
            (reviews / "review.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                                 encoding="utf-8")
            with (reviews / "audit.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            if items and all(i.get("verdict") == "approve" for i in items):
                meta_path = evalset / "meta.json"
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    meta = None
                if isinstance(meta, dict):
                    meta["reviewed_by"] = "human"
                    meta["reviewed_at"] = payload["reviewed_at"]
                    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            _refresh_reports_for_evalset(evalset)
            return _send_json(self, 200, {"ok": True, "path": str(reviews / "review.json")})
        if path == "/api/llm/test":
            data = _json_body(self)
            try:
                text = llm_layer.ask("只回复 OK", model=data.get("model") or None,
                                     base_url_override=data.get("base_url") or None,
                                     api_key_override=data.get("api_key") or None,
                                     protocol_override=data.get("protocol") or None)
                return _send_json(self, 200, {"ok": True, "text": text[:200]})
            except Exception as exc:
                return _send_json(self, 400, {"ok": False, "error": str(exc)})
        if path == "/api/override":
            data = _json_body(self)
            rid = str(data.get("id") or "")
            reason = str(data.get("reason") or "").strip()
            run = ACTIVE.get(rid)
            if not run:
                return _send_json(self, 404, {"error": "run not found"})
            if not reason:
                return _send_json(self, 400, {"error": "人工调整必须写原因"})
            report_path = run["results"] / "report.json"
            if not report_path.is_file():
                return _send_json(self, 400, {"error": "报告还没生成"})
            report = json.loads(report_path.read_text(encoding="utf-8"))
            metrics = report.get("metrics", {})
            raw_changes = data.get("changes")
            if not isinstance(raw_changes, list) or not raw_changes:
                metric = str(data.get("metric") or "")
                raw_changes = [{"metric": metric, "to": str(data.get("to") or "warn")}] if metric else []
            changes = {}
            for item in raw_changes:
                if not isinstance(item, dict):
                    continue
                metric = str(item.get("metric") or "")
                target = str(item.get("to") or "")
                if metric not in metrics:
                    return _send_json(self, 400, {"error": f"未知指标：{metric}"})
                if target not in ("pass", "warn", "fail", "skipped"):
                    return _send_json(self, 400, {"error": f"未知裁决：{target}"})
                current = (metrics.get(metric) or {}).get("verdict")
                if target == current:
                    continue
                changes[metric] = {"from": current, "to": target}
            if not changes:
                return _send_json(self, 400, {"error": "没有实际调整"})
            ov_path = run["results"] / "overrides.json"
            try:
                overrides = json.loads(ov_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                overrides = {}
            reviewer = str(data.get("reviewer") or "human")
            at = time.strftime("%Y-%m-%dT%H:%M:%S")
            for metric, change in changes.items():
                overrides[metric] = {**change, "reason": reason, "reviewer": reviewer, "at": at}
            ov_path.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")
            with (run["results"] / "manual-overrides.jsonl").open("a", encoding="utf-8") as fh:
                for metric, change in changes.items():
                    fh.write(json.dumps({"metric": metric, **overrides[metric]},
                                        ensure_ascii=False) + "\n")
            try:
                return _send_json(self, 200, _rerun_report(run))
            except Exception as exc:
                return _send_json(self, 500, {"error": str(exc)})
        if path != "/api/run":
            return _send_json(self, 404, {"error": "not found"})
        data = _json_body(self)
        skill = str(data.get("skill") or "").strip()
        if not skill or not (Path(skill) / "SKILL.md").is_file():
            return _send_json(self, 400, {"error": "skill 路径下没有 SKILL.md"})
        tier = str(data.get("tier") or "t2a").lower()
        if tier not in ("t0", "t1", "t2a", "t2b", "t3"):
            return _send_json(self, 400, {"error": "tier 只支持 t0/t1/t2a/t2b/t3"})
        rid = uuid.uuid4().hex[:12]
        rdir = RUNS / rid
        rdir.mkdir(parents=True, exist_ok=True)
        progress = rdir / "progress.ndjson"
        results = rdir / "results"
        reuse_from = str(data.get("reuse_from") or "")
        if reuse_from:
            prev = ACTIVE.get(reuse_from)
            if prev and prev["proc"].poll() is not None:
                _copy_reuse(prev["results"], results)
        llm_cfg = data.get("llm") if isinstance(data.get("llm"), dict) else {}
        llm_model = str(llm_cfg.get("model") or data.get("model") or "")
        backend = str(data.get("backend") or ("llm" if llm_cfg else "auto"))
        cmd = [sys.executable, str(RUNNER), "--skill", skill, "--tier", tier,
               "--results", str(results), "--progress", str(progress),
               "--backend", backend]
        if llm_model:
            cmd += ["--model", llm_model]
        if llm_cfg.get("protocol"):
            cmd += ["--llm-protocol", str(llm_cfg["protocol"])]
        if llm_cfg.get("base_url"):
            cmd += ["--base-url", str(llm_cfg["base_url"])]
        if llm_cfg.get("api_key"):
            cmd += ["--api-key", str(llm_cfg["api_key"])]
        if data.get("evalset"):
            cmd += ["--evalset", str(data["evalset"])]
        if data.get("harness"):
            cmd += ["--harness", str(data["harness"])]
        if data.get("rerun_t0"):
            cmd += ["--rerun-t0"]
        if data.get("rerun_t1"):
            cmd += ["--rerun-t1"]
        if data.get("rerun_t2a"):
            cmd += ["--rerun-t2a"]
        if data.get("rerun_t2b"):
            cmd += ["--rerun-t2b"]
        if data.get("rerun_t3"):
            cmd += ["--rerun-t3"]
        if data.get("no_cache"):
            cmd += ["--no-cache"]
        real_cases = data.get("real_cases")
        if isinstance(real_cases, list):
            rows = []
            for i, row in enumerate(real_cases, 1):
                if not isinstance(row, dict):
                    return _send_json(self, 400, {"error": f"真实输入第 {i} 条不是对象"})
                prompt = str(row.get("prompt") or "").strip()
                if not prompt:
                    return _send_json(self, 400, {"error": f"真实输入第 {i} 条缺 prompt"})
                rows.append({"id": f"c{i:03d}", "prompt": prompt,
                             "note": str(row.get("note") or row.get("label") or "").strip(),
                             "expect": str(row.get("expect") or "").strip()})
            if len(rows) > 20:
                return _send_json(self, 400, {"error": "真实输入最多 20 条"})
            if rows:
                rf = rdir / "real-input.jsonl"
                rf.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                              encoding="utf-8")
                cmd += ["--real-input", str(rf)]
        else:
            real = str(data.get("real_input") or "").strip()
            if real:
                rf = rdir / "real-input.json"
                try:
                    json.loads(real)
                    rf.write_text(real, encoding="utf-8")
                except json.JSONDecodeError:
                    rf.write_text(json.dumps({"prompt": real, "label": "real-input"},
                                             ensure_ascii=False), encoding="utf-8")
                cmd += ["--real-input", str(rf)]
        log = (rdir / "run.log").open("w", encoding="utf-8")
        popen_kwargs = {"cwd": ROOT, "stdout": log, "stderr": subprocess.STDOUT,
                        "text": True, "encoding": "utf-8", "errors": "replace"}
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(cmd, **popen_kwargs)
        ACTIVE[rid] = {"proc": proc, "dir": rdir, "results": results,
                       "progress": progress, "log": log, "stopped": False}
        (rdir / "run.json").write_text(json.dumps({
            "id": rid, "skill": skill, "skill_name": _skill_name(Path(skill)),
            "workspace": str(data.get("workspace") or ""), "tier": tier,
            "evalset": str(data.get("evalset") or ""),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
            ensure_ascii=False, indent=2), encoding="utf-8")
        return _send_json(self, 200, {"id": rid, "results": str(results),
                                      "skill_name": _skill_name(Path(skill))})

    def _events(self, rid: str):
        run = ACTIVE.get(rid)
        if not run:
            return _send_json(self, 404, {"error": "run not found"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        pos = 0
        last_ping = time.time()
        # 不设硬截止：长跑可能数小时；客户端断开由写失败/前端重连处理。
        while True:
            path = run["progress"]
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                if len(text) > pos:
                    chunk = text[pos:]
                    pos = len(text)
                    for line in chunk.splitlines():
                        if not line.strip():
                            continue
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        self.wfile.write(("data: " + line + "\n\n").encode("utf-8"))
                        self.wfile.flush()
                        if ev.get("type") in ("done", "error", "stopped"):
                            # stopped 由 /api/stop 在局部报告生成后写入，收到即收尾。
                            return
            if run["proc"].poll() is not None:
                if run.get("stopped"):
                    # 兜底：进程先于 stopped 事件退出（或事件写盘失败）时，
                    # 等 /api/stop 完成局部报告再用运行态补发一条。
                    waited = 0.0
                    while not run.get("finalized") and waited < 30:
                        time.sleep(0.1)
                        waited += 0.1
                    ev = {"type": "stopped", "ts": time.time(),
                          "clock": time.strftime("%H:%M:%S"),
                          "report": run.get("report") or "",
                          "partial": bool(run.get("report")),
                          "completed_stages": run.get("completed_stages") or [],
                          "error": run.get("stop_error") or "用户终止评估"}
                    self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode("utf-8"))
                else:
                    ev = {"type": "error", "error": "运行已结束但没有产出报告",
                          "log": str(run.get("dir", "") / "run.log")}
                    self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
                return
            if time.time() - last_ping >= 15:
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                last_ping = time.time()
            try:
                time.sleep(0.25)
            except (BrokenPipeError, ConnectionResetError):
                return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    ns = parser.parse_args()
    srv = ThreadingHTTPServer((ns.host, ns.port), Handler)
    srv.daemon_threads = True
    print(f"skill-evaluator web: http://{ns.host}:{ns.port}  (Ctrl+C 停止)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
