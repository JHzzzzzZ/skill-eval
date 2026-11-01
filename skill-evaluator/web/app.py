"""前端入口：stdlib HTTP 服务 + 原生 JS，只绑 127.0.0.1。

用法：python web/app.py [--port 8765]
页面选档位/执行载体/模型/评测集/真实输入 → 后台跑 scripts/runner.py → SSE 推进度。
"""
from __future__ import annotations

import argparse
import base64
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


def _safe_extract(zip_path: Path, dest: Path) -> None:
    """解压 zip：拒绝绝对路径/.. /符号链接；限制文件数与总解压体积。"""
    if zip_path.stat().st_size > MAX_UPLOAD_BYTES:
        raise ValueError("压缩包超过 1GB")
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_UPLOAD_FILES:
            raise ValueError(f"压缩包文件数超过 {MAX_UPLOAD_FILES}")
        total = 0
        for info in infos:
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in Path(name).parts:
                raise ValueError(f"压缩包含非法路径：{name}")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError(f"压缩包含符号链接：{name}")
            total += info.file_size
            if total > MAX_EXTRACTED_BYTES:
                raise ValueError("解压后体积超过 2GB")
        zf.extractall(dest)


def _receive_zip(handler) -> tuple[str, Path, Path]:
    """流式接收原始 zip（浏览器直接 POST File，不走 base64/JSON）。"""
    q = parse_qs(urlparse(handler.path).query)
    filename = (q.get("filename") or [""])[0]
    if not filename.lower().endswith(".zip"):
        raise ValueError("只支持 .zip")
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        raise ValueError("缺少 Content-Length")
    if length > MAX_UPLOAD_BYTES:
        handler.close_connection = True
        raise ValueError("压缩包超过 1GB")
    uid = uuid.uuid4().hex[:12]
    dest = RUNS / "uploads" / uid
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
    """停止一次运行，并写一条 stopped 事件让 SSE 立刻收尾。"""
    _stop_process(run["proc"])
    run["stopped"] = True
    event = {"type": "stopped", "ts": time.time(),
             "clock": time.strftime("%H:%M:%S"), "error": "用户终止评估"}
    try:
        with run["progress"].open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _slug(text: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", str(text or "")).strip("-._").lower()
    return value or "skill"


def _evalset_for(name: str, sha: str) -> Path:
    base = ROOT.parent / "evalsets"
    slug = _slug(name) if _slug(name) != "skill" else f"skill-{sha[:8]}"
    d = base / slug
    versions = sorted((p for p in d.glob("v*") if p.is_dir()),
                      key=lambda p: int(p.name[1:]) if p.name[1:].isdigit() else 0)
    return versions[-1] if versions else d / "v1"


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
            "evalset": str(_evalset_for(name, sha)), "skill_sha256": sha}


def _workspaces() -> list:
    out = []
    for ws in sorted(WORKSPACES.iterdir()):
        if ws.is_dir() and (ws / "workspace.json").is_file():
            meta = _workspace_meta(ws)
            meta["evalset"] = str(_evalset_for(meta.get("name") or ws.name,
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


def _rerun_report(run) -> dict:
    meta = {}
    try:
        meta = json.loads((run["results"] / "run-meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    cmd = [sys.executable, str(ROOT / "scripts" / "report.py"), str(run["results"]),
           "--skill", meta.get("skill", ""), "--tier", meta.get("tier", ""), "--html"]
    if meta.get("evalset"):
        cmd += ["--evalset", meta["evalset"]]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-800:])
    return json.loads((run["results"] / "report.json").read_text(encoding="utf-8"))


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
            run = ACTIVE.get(rid)
            if not run:
                return _send_json(self, 404, {"error": "run not found"})
            return _send_json(self, 200, {
                "id": rid, "running": run["proc"].poll() is None,
                "returncode": run["proc"].poll(), "stopped": bool(run.get("stopped")),
                "results": str(run["results"]), "report": str(run["results"] / "report.html")})
        if path == "/api/report":
            q = parse_qs(urlparse(self.path).query)
            rid = (q.get("id") or [""])[0]
            run = ACTIVE.get(rid)
            if not run:
                return _send_json(self, 404, {"error": "run not found"})
            report = run["results"] / "report.json"
            if not report.is_file():
                return _send_json(self, 404, {"error": "report not ready"})
            return _send_json(self, 200, json.loads(report.read_text(encoding="utf-8")))
        if path == "/report":
            q = parse_qs(urlparse(self.path).query)
            rid = (q.get("id") or [""])[0]
            run = ACTIVE.get(rid)
            if not run:
                return _send_json(self, 404, {"error": "run not found"})
            html_path = run["results"] / "report.html"
            if not html_path.is_file():
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
            return _send_json(self, 200, {"ok": True, "id": rid,
                                          "running": run["proc"].poll() is None,
                                          "stopped": bool(run.get("stopped"))})
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
                return _send_json(self, 200, json.loads(r.stdout))
            except json.JSONDecodeError:
                return _send_json(self, 500, {"error": r.stdout[-800:]})
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
                       "progress": progress, "log": log}
        return _send_json(self, 200, {"id": rid, "results": str(results)})

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
        deadline = time.time() + 60 * 60
        while time.time() < deadline:
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
                            return
            if run["proc"].poll() is not None:
                if run.get("stopped"):
                    ev = {"type": "stopped", "ts": time.time(),
                          "clock": time.strftime("%H:%M:%S"), "error": "用户终止评估"}
                    self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode("utf-8"))
                else:
                    self.wfile.write(b'data: {"type":"error","error":"runner exited"}\n\n')
                self.wfile.flush()
                return
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
