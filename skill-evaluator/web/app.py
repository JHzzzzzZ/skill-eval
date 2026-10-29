"""前端入口（ADR-0036）：stdlib HTTP 服务 + 原生 JS，只绑 127.0.0.1。

用法：python web/app.py [--port 8765]
页面选档位/执行载体/模型/评测集/真实输入 → 后台跑 scripts/runner.py → SSE 推进度。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB = Path(__file__).resolve().parent
ROOT = WEB.parent
RUNNER = ROOT / "scripts" / "runner.py"
RUNS = WEB / "runs"
RUNS.mkdir(parents=True, exist_ok=True)
ACTIVE = {}


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
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/events":
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            rid = (q.get("id") or [""])[0]
            return self._events(rid)
        if path == "/api/status":
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            rid = (q.get("id") or [""])[0]
            run = ACTIVE.get(rid)
            if not run:
                return _send_json(self, 404, {"error": "run not found"})
            return _send_json(self, 200, {
                "id": rid, "running": run["proc"].poll() is None,
                "returncode": run["proc"].poll(), "results": str(run["results"]),
                "report": str(run["results"] / "report.html")})
        if path == "/api/report":
            from urllib.parse import parse_qs, urlparse
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
            from urllib.parse import parse_qs, urlparse
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
        if self.path.split("?", 1)[0] != "/api/run":
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
        cmd = [sys.executable, str(RUNNER), "--skill", skill, "--tier", tier,
               "--results", str(results), "--progress", str(progress),
               "--backend", str(data.get("backend") or "auto")]
        if data.get("model"):
            cmd += ["--model", str(data["model"])]
        if data.get("evalset"):
            cmd += ["--evalset", str(data["evalset"])]
        if data.get("harness"):
            cmd += ["--harness", str(data["harness"])]
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
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
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
                        if ev.get("type") in ("done", "error"):
                            return
            if run["proc"].poll() is not None:
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
