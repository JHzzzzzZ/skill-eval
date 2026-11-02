"""确定性评估编排（ADR-0036）：T0 → T1 → T2A → T2B → T3 → report。

前端与 agent 共用本入口；阶段按 --tier 短路，进度以 NDJSON 输出（--progress 另落文件）。
不重写各脚本的测量逻辑：只负责串流程、落中间产物、报告进度。
检查点只在阶段边界：阶段完成（产物可读且必要字段齐备）才写缓存；
半截 T1/T2A/T2B/T3 不续跑，下次整档重跑，之前完整阶段照常复用。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import _console

_console.fix()

import _config
import llm as llm_layer
import report as report_mod

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
CACHE_ROOT = ROOT.parent / ".skill-eval-cache"
CACHE = CACHE_ROOT / "t0t1"
TIERS = ("t0", "t1", "t2a", "t2b", "t3")
TIER_RANK = {name: i for i, name in enumerate(TIERS)}
EXCLUDE = {"tests", "evalsets", "uploads", "attic", ".git", "node_modules",
           "__pycache__", ".sandbox", ".skillrepos", ".pytest_cache", "runs", "cache", "calibration", "workspaces"}
RUBRICS = ("brevity", "redundancy", "fallback", "precheck", "contract", "deps", "pii")
GUARD = [ROOT.parent] if (ROOT.parent / ".git").exists() else []


class RunnerError(RuntimeError):
    pass


class Progress:
    def __init__(self, path: str | None = None):
        self.path = Path(path) if path else None
        self.started: dict[str, float] = {}
        self.t0 = time.monotonic()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("", encoding="utf-8")

    def emit(self, kind: str, **fields):
        now = time.time()
        fields.setdefault("clock", time.strftime("%H:%M:%S", time.localtime(now)))
        if kind == "stage":
            name = str(fields.get("name") or "")
            status = fields.get("status")
            if status == "start":
                self.started[name] = time.monotonic()
            elif status in ("done", "skipped"):
                begin = self.started.pop(name, None)
                fields.setdefault("elapsed_s", round(time.monotonic() - begin, 2) if begin else 0.0)
        if kind in ("done", "error"):
            fields.setdefault("elapsed_s", round(time.monotonic() - self.t0, 2))
        event = {"type": kind, "ts": now, **fields}
        line = json.dumps(event, ensure_ascii=False)
        print(line, flush=True)
        if self.path:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")


def _read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _run(name: str, args: list, *, timeout=None, check=True) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(SCRIPTS / name), *map(str, args)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout or _config.get_int("run.timeout_s", 300))
    if check and proc.returncode != 0:
        raise RunnerError(f"{name} 失败（{proc.returncode}）: {proc.stderr[-800:]}")
    return proc


def _json_script(name: str, args: list, *, timeout=None) -> dict:
    proc = _run(name, args, timeout=timeout)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RunnerError(f"{name} 输出不是 JSON: {proc.stdout[:300]}") from exc


def _copy_skill(src: Path, dst: Path) -> Path:
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    def ignore(dirname, names):
        return [n for n in names if n in EXCLUDE]

    shutil.copytree(src, dst, ignore=ignore)
    for cmd in (["git", "init", "-q"], ["git", "config", "core.autocrlf", "false"],
                ["git", "add", "-A"], ["git", "commit", "-qm", "snapshot"]):
        try:
            subprocess.run(cmd, cwd=dst, capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            break
    return dst


def _skill_digest(skill: Path) -> str:
    """与被复制进沙箱的内容一致：排除测试/评测集/运行产物，路径 + 内容参与哈希。"""
    h = hashlib.sha256()
    for p in sorted(skill.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(skill)
        if any(part in EXCLUDE for part in rel.parts) or p.suffix == ".pyc":
            continue
        try:
            h.update(rel.as_posix().encode("utf-8"))
            h.update(b"\x00")
            h.update(p.read_bytes())
        except OSError:
            continue
    return h.hexdigest()


def _judges_ready(jdir: Path) -> bool:
    """T1 阶段完成判定：7 个 rubric 全部可读且契约合法才算完成。

    检查点只在阶段边界：少一个 rubric 就整档重跑，不复制半截评审结果。
    """
    if not jdir.is_dir():
        return False
    for metric in RUBRICS:
        data = _read_json(jdir / f"{metric}.json", {})
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            return False
        score = data.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
            return False
    return True


def _t0_ready(d: Path) -> bool:
    static = _read_json(d / "static.json", None)
    return isinstance(static, dict) and isinstance(static.get("passed"), bool)


def _t2a_ready(d: Path) -> bool:
    triggers = _read_json(d / "triggers.json", None)
    if not isinstance(triggers, dict):
        return False
    if not all(isinstance(triggers.get(k), list) for k in ("should", "should_not", "confusable")):
        return False
    score = _read_json(d / "score.json", None)
    return isinstance(score, dict) and "trigger" in score


def _t2b_ready(d: Path) -> bool:
    score = _read_json(d / "t2b" / "score.json", None)
    if not isinstance(score, dict) or "necessity" not in score:
        return False
    for name in ("runs.json", "baseline.json"):
        if not isinstance(_read_json(d / "t2b" / name, None), list):
            return False
    return True


def _t3_ready(d: Path) -> bool:
    if not isinstance(_read_json(d / "runs.json", None), list):
        return False
    score = _read_json(d / "score.json", None)
    return isinstance(score, dict) and all(k in score for k in ("trigger", "cost", "necessity"))


def stage_ready(name: str, d: Path) -> bool:
    """阶段完成判定：只看产物且校验必要字段，文件存在不等于阶段完成。"""
    checks = {"t0": _t0_ready, "t1": lambda p: _judges_ready(p / "judges"),
              "t2a": _t2a_ready, "t2b": _t2b_ready, "t3": _t3_ready}
    fn = checks.get(str(name).lower())
    return bool(fn and fn(Path(d)))


def _cache_match(old: dict, skill_sha: str, eval_sha, judge_key: dict | None = None) -> bool:
    if not isinstance(old, dict) or not eval_sha:
        return False
    if old.get("skill_sha256") != skill_sha or old.get("evaluator_sha256") != eval_sha:
        return False
    if judge_key is not None and old.get("judge") != judge_key:
        return False
    return True


def _dir_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in {"reviews", "results", "__pycache__"} for part in rel.parts):
            continue
        h.update(rel.as_posix().encode("utf-8"))
        h.update(b"\x00")
        h.update(p.read_bytes())
    return h.hexdigest()


def _stage_cache(name: str, *parts) -> Path:
    digest = hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True)
                            .encode("utf-8")).hexdigest()[:32]
    return CACHE_ROOT / name / digest


def _cache_dir(skill_sha: str, eval_sha: str, judge_key: dict | None = None) -> Path:
    raw = f"{skill_sha}:{eval_sha or 'unknown'}"
    if judge_key is not None:
        raw += ":" + json.dumps(judge_key, sort_keys=True, ensure_ascii=False)
    return CACHE / hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _copy_cache(src: Path, dst: Path, files=(), dirs=()) -> bool:
    """把缓存产物复制进 results；返回是否真的复制了东西。"""
    if not src.is_dir():
        return False
    copied = False
    dst.mkdir(parents=True, exist_ok=True)
    for name in files:
        p = src / name
        if p.is_file():
            shutil.copy2(p, dst / name)
            copied = True
    for rel in dirs:
        p = src / rel
        if p.is_dir():
            shutil.copytree(p, dst / rel, dirs_exist_ok=True)
            copied = True
    return copied


def _find_evalset(name: str) -> Path | None:
    for base in (ROOT / "evalsets", ROOT.parent / "evalsets"):
        d = base / name
        versions = sorted((p for p in d.glob("v*") if p.is_dir()),
                          key=lambda p: int(p.name[1:]) if p.name[1:].isdigit() else 0)
        if versions:
            return versions[-1]
    return None


def _prompts(evalset: Path) -> dict:
    groups = {"should": "should", "should_not": "should-not", "confusable": "confusable"}
    out = {}
    for key, sub in groups.items():
        vals = []
        for f in sorted((evalset / "triggers" / sub).glob("*.json")):
            data = _read_json(f, {})
            if isinstance(data, dict) and isinstance(data.get("prompt"), str) and data["prompt"].strip():
                vals.append({"file": f"{sub}/{f.name}", "prompt": data["prompt"]})
        out[key] = vals
    return out


MAX_REAL_CASES = 20


def _real_cases(path: Path) -> list:
    """解析真实输入：JSONL（一行一条）/ JSON 数组 / 单对象 / 纯文本。

    只有 prompt 必填；id 一律由后端重编为 c001…，避免用户手填 ID。
    note 是展示用备注；expect 是验收标准，可选。
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    rows = None
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            rows = [data]
        elif isinstance(data, list):
            rows = data
        else:
            rows = [{"prompt": raw}]
    except json.JSONDecodeError:
        parsed = []
        bad = None
        for lineno, line in enumerate(raw.splitlines(), 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                bad = (lineno, str(exc))
                break
            parsed.append(obj)
        if bad:
            # 不是 JSONL 的纯文本按单条处理；只有看起来像 JSON 的坏行才报错
            if raw.lstrip().startswith(("{", "[")):
                raise RunnerError(f"真实输入第 {bad[0]} 行不是合法 JSON：{bad[1]}")
            rows = [{"prompt": raw}]
        else:
            rows = parsed or [{"prompt": raw}]
    if not isinstance(rows, list) or not rows:
        raise RunnerError("真实输入为空")
    if len(rows) > MAX_REAL_CASES:
        raise RunnerError(f"真实输入最多 {MAX_REAL_CASES} 条，当前 {len(rows)} 条")
    cases = []
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise RunnerError(f"真实输入第 {i} 条不是对象")
        prompt = str(row.get("prompt") or "").strip()
        if not prompt:
            raise RunnerError(f"真实输入第 {i} 条缺 prompt")
        note = str(row.get("note") or row.get("label") or "").strip()
        expect = str(row.get("expect") or "").strip()
        cases.append({"id": f"c{i:03d}", "prompt": prompt,
                      "note": note, "expect": expect})
    return cases


def _cases(evalset: Path) -> list:
    out = []
    for f in sorted((evalset / "cases").glob("*.json")):
        data = _read_json(f, {})
        if isinstance(data, dict) and isinstance(data.get("prompt"), str) and data["prompt"].strip():
            out.append({"name": f.stem, "prompt": data["prompt"],
                        "expect": str(data.get("expect") or "")})
    return out


def _env_error(trace: dict) -> bool:
    """pi/载体级失败不算“未触发”，否则 P/R 会被环境错误打成 0。"""
    markers = ("pi 退出码", "pi CLI", "运行超时", "触发上限", "worktree", "provider")
    return any(any(m in str(e) for m in markers) for e in (trace.get("errors") or []))


def _metric(trace: dict, case: str) -> dict:
    return {"case": case, "tool_calls": int(trace.get("tool_calls") or 0),
            "tokens": int(trace.get("tokens") or 0),
            "seconds": float(trace.get("seconds") or 0.0)}


def _merge_necessity(score_path: Path, t2b_score: Path | None):
    if not t2b_score or not t2b_score.is_file():
        return
    score = _read_json(score_path, {}) or {}
    t2b = _read_json(t2b_score, {}) or {}
    if t2b.get("necessity") != "skipped":
        score["necessity"] = t2b["necessity"]
        _write_json(score_path, score)


def run(args) -> int:
    tier = args.tier.lower()
    if tier not in TIER_RANK:
        raise RunnerError(f"--tier 只支持 {', '.join(TIERS)}")
    skill = Path(args.skill).resolve()
    if not (skill / "SKILL.md").is_file():
        raise RunnerError(f"不是合法 skill 包（缺 SKILL.md）: {skill}")
    progress = Progress(args.progress)
    stages = [t for t in TIERS[:TIER_RANK[tier] + 1]]
    progress.emit("plan", tier=tier, stages=stages, skill=str(skill))
    if args.dry_run:
        return 0

    version = args.version or time.strftime("%Y%m%d-%H%M%S")
    results = Path(args.results).resolve() if args.results else \
        ROOT / "evalsets" / skill.name / "results" / f"auto-{version}"
    results.mkdir(parents=True, exist_ok=True)
    static_path = results / "static.json"
    skill_sha = _skill_digest(skill)
    eval_info = report_mod.evaluator_fingerprint(ROOT)
    eval_sha = eval_info.get("content_sha256")
    cache_t0 = _cache_dir(skill_sha, eval_sha)
    static_from_cache = False
    if not args.rerun_t0 and not static_path.is_file():
        static_from_cache = _copy_cache(cache_t0, results, ("static.json",))
    old_meta = _read_json(results / "run-meta.json", {}) or {}
    progress.emit("stage", name="T0", status="start")

    # T0 静态检查：同 skill + 同评估器才复用，否则重跑
    static = _read_json(static_path, {}) or {}
    reuse_static = (not args.no_cache and not args.rerun_t0 and bool(static)
                    and isinstance(static.get("passed"), bool)
                    and (static_from_cache or _cache_match(old_meta, skill_sha, eval_sha)))
    if not reuse_static:
        _run("static_check.py", [skill, "--out", static_path])
        static = _read_json(static_path, {}) or {}
    if isinstance(static.get("passed"), bool):
        _copy_cache(results, cache_t0, ("static.json",))
    name = static.get("name") or skill.name
    human = bool(static.get("human")) or (static.get("invoke") or {}).get("resolved") == "human"
    progress.emit("stage", name="T0", status="done", passed=static.get("passed"),
                  cached=reuse_static)

    evalset = Path(args.evalset).resolve() if args.evalset else _find_evalset(name)
    if evalset and not evalset.is_dir():
        raise RunnerError(f"评测集不存在: {evalset}")
    evalset_sha = _dir_digest(evalset) if evalset else ""
    model = args.model or os.environ.get("SKILL_EVAL_MODEL") or None
    judge_backend = ("llm" if (args.base_url or args.api_key or args.llm_protocol) else args.backend)
    if judge_backend == "auto":
        judge_backend = "llm" if llm_layer.configured() else "pi"
    judge_key = {"backend": judge_backend, "model": model or "",
                 "protocol": args.llm_protocol or "", "base_url": args.base_url or ""}
    _write_json(results / "run-meta.json", {
        "skill": str(skill), "skill_name": name, "tier": tier,
        "evalset": str(evalset) if evalset else None,
        "evalset_version": evalset.name if evalset else None,
        "model": model, "harness": args.harness, "llm_protocol": args.llm_protocol,
        "base_url": args.base_url, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "skill_sha256": skill_sha, "evaluator_sha256": eval_sha,
        "evaluator_version": eval_info.get("version"), "judge": judge_key,
    })

    # T1 评审（默认 llm；未配置时 auto 回落 pi）
    if TIER_RANK[tier] >= TIER_RANK["t1"]:
        progress.emit("stage", name="T1", status="start")
        jdir = results / "judges"
        jdir.mkdir(parents=True, exist_ok=True)
        cache_t1 = _cache_dir(skill_sha, eval_sha, judge_key)
        # 检查点只在阶段完成时写：缓存里必须 7 个 rubric 全齐，半截结果不复制。
        judges_from_cache = False
        if not args.no_cache and not args.rerun_t1 and _judges_ready(cache_t1 / "judges"):
            judges_from_cache = _copy_cache(cache_t1, results, (), ("judges", "raw/llm-judge"))
        reuse_judges = (not args.no_cache and not args.rerun_t1
                        and _judges_ready(jdir)
                        and (judges_from_cache or _cache_match(old_meta, skill_sha, eval_sha, judge_key)))
        if not reuse_judges:
            # 当前阶段没跑完，或缓存/结果不匹配：整档重跑，不保留半截 rubric。
            shutil.rmtree(jdir, ignore_errors=True)
            shutil.rmtree(results / "raw" / "llm-judge", ignore_errors=True)
            jdir.mkdir(parents=True, exist_ok=True)
        if reuse_judges:
            progress.emit("stage", name="T1", status="done", cached=True)
        else:
            workers = max(1, _config.get_int("run.judge_concurrency", 4))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {}
                judge_started = {}
                for metric in RUBRICS:
                    judge_started[metric] = time.monotonic()
                    futs[pool.submit(_run, "judge_run.py",
                                     ["--metric", metric, "--skill", skill,
                                      "--out", jdir / f"{metric}.json", "--backend", judge_backend,
                                      "--raw-dir", results / "raw" / "llm-judge",
                                      "--model", model or "", "--protocol", args.llm_protocol or "",
                                      "--base-url", args.base_url or "", "--api-key", args.api_key or ""])] = metric
                for fut in as_completed(futs):
                    metric = futs[fut]
                    elapsed = round(time.monotonic() - judge_started.get(metric, time.monotonic()), 2)
                    try:
                        fut.result()
                        progress.emit("judge", metric=metric, status="done", elapsed_s=elapsed)
                    except Exception as exc:
                        progress.emit("judge", metric=metric, status="skipped", elapsed_s=elapsed,
                                      error=str(exc))
        if _judges_ready(jdir):
            # 阶段完成才落缓存；下次整档复用。
            _copy_cache(results, cache_t1, (), ("judges", "raw/llm-judge"))
        if not reuse_judges:
            progress.emit("stage", name="T1", status="done", cached=False,
                          cache_reason="首次运行或评估器/模型变化",
                          incomplete=not _judges_ready(jdir))

    # T2+ 沙箱
    sandbox = None
    if TIER_RANK[tier] >= TIER_RANK["t2a"]:
        sandbox = Path(args.sandbox).resolve() if args.sandbox else results / "sandbox"
        if not args.sandbox:
            progress.emit("stage", name="sandbox", status="start")
            _copy_skill(skill, sandbox)
            progress.emit("stage", name="sandbox", status="done", path=str(sandbox))

    if TIER_RANK[tier] >= TIER_RANK["t2a"]:
        progress.emit("stage", name="T2A", status="start")
        t2a_cache = _stage_cache("t2a", skill_sha, eval_sha, evalset_sha,
                                 model or "", args.harness or "",
                                 args.llm_protocol or "", args.base_url or "")
        reuse_t2a = (not args.no_cache and not args.rerun_t2a
                     and _t2a_ready(t2a_cache))
        if reuse_t2a:
            _copy_cache(t2a_cache, results, ("triggers.json", "score.json"),
                        ("triggers", "trigger-judge"))
            progress.emit("stage", name="T2A", status="done", cached=True)
        elif human:
            progress.emit("stage", name="T2A", status="skipped",
                          reason="human 型：不跑触发评测")
        elif not evalset:
            progress.emit("stage", name="T2A", status="skipped", reason="无评测集")
        else:
            # 本次整档重跑：先清掉旧缓存，失败时不会留下被误认为完整的半截产物。
            shutil.rmtree(t2a_cache, ignore_errors=True)
            for stale in ("triggers.json", "score.json"):
                (results / stale).unlink(missing_ok=True)
            for stale in ("triggers", "trigger-judge"):
                shutil.rmtree(results / stale, ignore_errors=True)
            groups = _prompts(evalset)
            judged = {"should": [], "should_not": [], "confusable": []}
            trigger_timeout = max(1, _config.get_int("run.trigger_timeout_s", 90))
            outer_timeout = trigger_timeout + 30  # 留给 trace_run 杀树/落盘，不先被外层打断
            total = sum(len(v) for v in groups.values())
            idx = 0
            from harness import run_case  # 延迟导入：dry-run 不需要
            for key, items in groups.items():
                for item in items:
                    idx += 1
                    progress.emit("case", stage="T2A", index=idx, total=total, label=item["file"])
                    trace_path = results / "triggers" / f"{key}-{idx}.json"
                    try:
                        trace = run_case(item["prompt"], sandbox, skill_dir=sandbox, adapter=args.harness,
                                         early_exit=True, model=model, out=trace_path,
                                         timeout=outer_timeout)
                        if _env_error(trace):
                            progress.emit("warn", stage="T2A", label=item["file"],
                                          error="载体失败，跳过该 case：" + str(trace.get("errors"))[:200])
                            continue
                        if trace.get("early_exit"):
                            # 已读到 SKILL.md：按“加载即触发”直接裁决，不再花一次 LLM
                            judged[key].append(True)
                            progress.emit("judge", stage="T2A", metric=item["file"],
                                          status="done", elapsed_s=0.0, loaded=True)
                            continue
                        jout = results / "trigger-judge" / f"{key}-{idx}.json"
                        judge_started = time.monotonic()
                        _run("trigger_judge.py", ["--skill", sandbox / "SKILL.md",
                                                  "--trace", trace_path, "--out", jout,
                                                  "--model", model or "",
                                                  "--base-url", args.base_url or "",
                                                  "--api-key", args.api_key or "",
                                                  "--llm-protocol", args.llm_protocol or ""])
                        judged[key].append(bool((_read_json(jout, {}) or {}).get("triggered")))
                        progress.emit("judge", stage="T2A", metric=item["file"],
                                      status="done", loaded=False,
                                      elapsed_s=round(time.monotonic() - judge_started, 2))
                    except subprocess.TimeoutExpired:
                        # 外层兜底超时：跳过而不是记成“未触发”，否则 should-not 会被误判
                        progress.emit("warn", stage="T2A", label=item["file"],
                                      error=f"运行超时（>{outer_timeout}s），跳过该 case")
                        continue
                    except Exception as exc:
                        judged[key].append(False)
                        progress.emit("warn", stage="T2A", label=item["file"], error=str(exc))
            _write_json(results / "triggers.json", judged)
            _run("score.py", [results, "--out", results / "score.json"], check=False)
            if _t2a_ready(results):
                _copy_cache(results, t2a_cache, ("triggers.json", "score.json"),
                            ("triggers", "trigger-judge"))
            progress.emit("stage", name="T2A", status="done",
                          incomplete=not _t2a_ready(results),
                          should=sum(judged["should"]), total_should=len(judged["should"]))

    # T2B 真实场景 A/B
    t2b_score = None
    real_input_sha = hashlib.sha256(Path(args.real_input).read_bytes()).hexdigest() if args.real_input else ""
    if TIER_RANK[tier] >= TIER_RANK["t2b"]:
        progress.emit("stage", name="T2B", status="start")
        t2b_cache = _stage_cache("t2b", skill_sha, eval_sha, evalset_sha, real_input_sha,
                                 model or "", args.harness or "",
                                 args.llm_protocol or "", args.base_url or "")
        reuse_t2b = (not args.no_cache and not args.rerun_t2b and bool(real_input_sha)
                     and _t2b_ready(t2b_cache))
        if reuse_t2b:
            _copy_cache(t2b_cache, results,
                        ("real-input.meta.json", "golden.json"), ("t2b",))
            t2b_score = results / "t2b" / "score.json"
            progress.emit("stage", name="T2B", status="done", cached=True)
        elif not args.real_input:
            progress.emit("stage", name="T2B", status="skipped", reason="未提供真实输入")
        else:
            shutil.rmtree(t2b_cache, ignore_errors=True)
            shutil.rmtree(results / "t2b", ignore_errors=True)
            for stale in ("real-input.meta.json", "golden.json"):
                (results / stale).unlink(missing_ok=True)
            from harness import run_case
            cases = _real_cases(Path(args.real_input))
            t2b = results / "t2b"
            cases_dir = t2b / "cases"
            cases_dir.mkdir(parents=True, exist_ok=True)
            digest = real_input_sha[:16]
            _write_json(results / "real-input.meta.json", {
                "sha256": real_input_sha, "note": cases[0]["note"],
                "count": len(cases),
                "cases": [{"id": c["id"], "note": c["note"],
                           "has_expect": bool(c["expect"])} for c in cases],
            })
            _write_json(ROOT / "uploads" / "real-input" / f"{digest}.json",
                        {"cases": cases})
            runs, baselines, compared, pairs = [], [], [], []
            valid = 0
            first_golden = None
            arm_timeout = max(1, _config.get_int("run.timeout_s", 300))

            def _map_winner(rec, first_is_with):
                winner = str((rec or {}).get("winner") or "").lower()
                if not (rec or {}).get("valid") or winner not in ("a", "b", "tie"):
                    return None
                if winner == "tie":
                    return "tie"
                return "with_skill" if (winner == "a") == first_is_with else "baseline"

            for idx, case in enumerate(cases, 1):
                cdir = cases_dir / case["id"]
                cdir.mkdir(parents=True, exist_ok=True)
                _write_json(cdir / "case.json", {
                    "id": case["id"], "note": case["note"],
                    "has_expect": bool(case["expect"]),
                })
                (cdir / "prompt.txt").write_text(case["prompt"], encoding="utf-8")
                display = case["id"] + (" · " + case["note"] if case["note"] else "")
                progress.emit("case", stage="T2B", index=idx, total=len(cases),
                              label=display, arm="with")
                gt = cdir / "with.json"
                bt = cdir / "baseline.json"
                gold = case["prompt"] if not human else f"/skill:{name} {case['prompt']}"
                try:
                    g = run_case(gold, sandbox, skill_dir=sandbox, model=model,
                                 out=gt, adapter=args.harness, guard_repo=GUARD,
                                 timeout=arm_timeout)
                    progress.emit("case", stage="T2B", index=idx, total=len(cases),
                                  label=display, arm="baseline")
                    b = run_case(case["prompt"], sandbox, no_skill=True, model=model,
                                 out=bt, adapter=args.harness, guard_repo=GUARD,
                                 timeout=arm_timeout)
                except subprocess.TimeoutExpired:
                    progress.emit("warn", stage="T2B", label=display,
                                  error=f"运行超时（>{arm_timeout}s），跳过该 case")
                    continue
                except Exception as exc:
                    progress.emit("warn", stage="T2B", label=display, error=str(exc))
                    continue
                valid += 1
                if first_golden is None:
                    first_golden = g
                runs.append(_metric(g, case["id"]))
                baselines.append(_metric(b, case["id"]))
                if case["expect"]:
                    actual = cdir / "actual.txt"
                    expect = cdir / "expect.txt"
                    actual.write_text(str(g.get("answer") or ""), encoding="utf-8")
                    expect.write_text(case["expect"], encoding="utf-8")
                    cout = t2b / "compare" / f"{case['id']}.json"
                    cout.parent.mkdir(parents=True, exist_ok=True)
                    _run("compare.py", ["--expect", str(expect), "--actual", str(actual),
                                        "--out", str(cout), "--model", model or "",
                                        "--base-url", args.base_url or "",
                                        "--api-key", args.api_key or "",
                                        "--llm-protocol", args.llm_protocol or ""],
                         check=False)
                    rec = _read_json(cout, {}) or {}
                    if isinstance(rec.get("match"), bool):
                        compared.append({"id": case["id"], **rec})
                else:
                    p1 = cdir / "pairwise-1.json"
                    p2 = cdir / "pairwise-2.json"
                    common = ["--prompt", str(cdir / "prompt.txt")]
                    _run("pairwise.py", common + ["--a", str(gt), "--b", str(bt),
                                                   "--out", str(p1), "--model", model or "",
                                                   "--base-url", args.base_url or "",
                                                   "--api-key", args.api_key or "",
                                                   "--llm-protocol", args.llm_protocol or ""],
                         check=False)
                    _run("pairwise.py", common + ["--a", str(bt), "--b", str(gt),
                                                   "--out", str(p2), "--model", model or "",
                                                   "--base-url", args.base_url or "",
                                                   "--api-key", args.api_key or "",
                                                   "--llm-protocol", args.llm_protocol or ""],
                         check=False)
                    m1 = _map_winner(_read_json(p1, {}) or {}, True)
                    m2 = _map_winner(_read_json(p2, {}) or {}, False)
                    if m1 and m2:
                        pairs.append({"id": case["id"],
                                      "winner": m1 if m1 == m2 else "tie",
                                      "judgment_1": m1, "judgment_2": m2})

            _write_json(t2b / "runs.json", runs)
            _write_json(t2b / "baseline.json", baselines)
            if compared:
                _run("compare.py", ["--aggregate", str(t2b / "compare"),
                                    "--out", str(t2b / "compare.json")], check=False)
            if pairs:
                with_wins = sum(1 for p in pairs if p["winner"] == "with_skill")
                base_wins = sum(1 for p in pairs if p["winner"] == "baseline")
                ties = sum(1 for p in pairs if p["winner"] == "tie")
                _write_json(t2b / "pairwise.json", {
                    "valid": len(pairs), "with_skill_wins": with_wins,
                    "baseline_wins": base_wins, "ties": ties,
                    "win_rate": round(with_wins / len(pairs), 3),
                    "cases": pairs,
                })
            if first_golden is not None:
                _write_json(results / "golden.json", first_golden)
            _run("score.py", [t2b, "--out", t2b / "score.json"])
            t2b_score = t2b / "score.json"
            if _t2b_ready(results):
                _copy_cache(results, t2b_cache,
                            ("real-input.meta.json", "golden.json"), ("t2b",))
            progress.emit("stage", name="T2B", status="done",
                          cases=len(cases), valid=valid, incomplete=not _t2b_ready(results))

    # T3 核心沙箱
    if TIER_RANK[tier] >= TIER_RANK["t3"]:
        progress.emit("stage", name="T3", status="start")
        cases = _cases(evalset) if evalset else []
        repeat = max(1, _config.get_int("run.repeat", 3))
        t3_cache = _stage_cache("t3", skill_sha, eval_sha, evalset_sha, real_input_sha,
                                model or "", args.harness or "", args.llm_protocol or "",
                                args.base_url or "", str(repeat))
        reuse_t3 = (not args.no_cache and not args.rerun_t3
                    and not args.rerun_t2a and not args.rerun_t2b
                    and _t3_ready(t3_cache))
        if reuse_t3:
            _copy_cache(t3_cache, results,
                        ("runs.json", "idem.json", "compare.json", "golden.json", "score.json"),
                        ("traces", "idem", "compare", "actual", "expected"))
            progress.emit("stage", name="T3", status="done", cached=True)
        elif not cases:
            progress.emit("stage", name="T3", status="skipped", reason="无 cases")
        else:
            shutil.rmtree(t3_cache, ignore_errors=True)
            for stale in ("runs.json", "idem.json", "compare.json", "score.json", "golden.json"):
                (results / stale).unlink(missing_ok=True)
            for stale in ("traces", "idem", "compare", "actual", "expected"):
                shutil.rmtree(results / stale, ignore_errors=True)
            from harness import run_case
            runs = []
            traces = {}
            total = len(cases) * repeat
            idx = 0
            for case in cases:
                traces[case["name"]] = []
                for rep in range(repeat):
                    idx += 1
                    progress.emit("case", stage="T3", index=idx, total=total,
                                  label=case["name"], repeat=rep + 1)
                    out = results / "traces" / f"golden-{case['name']}-{rep + 1}.json"
                    prompt = case["prompt"] if not human else f"/skill:{name} {case['prompt']}"
                    try:
                        trace = run_case(prompt, sandbox, skill_dir=sandbox, model=model, adapter=args.harness,
                                         out=out, guard_repo=GUARD)
                    except Exception as exc:
                        progress.emit("warn", stage="T3", label=case["name"], error=str(exc))
                        continue
                    traces[case["name"]].append(out)
                    runs.append(_metric(trace, case["name"]))
                if traces[case["name"]]:
                    shutil.copyfile(traces[case["name"]][0], results / "golden.json")
                if case["expect"]:
                    actual = results / "actual" / f"{case['name']}.txt"
                    expect = results / "expected" / f"{case['name']}.txt"
                    actual.parent.mkdir(parents=True, exist_ok=True)
                    expect.parent.mkdir(parents=True, exist_ok=True)
                    trace = _read_json(traces[case["name"]][0], {}) if traces[case["name"]] else {}
                    actual.write_text(str(trace.get("answer") or ""), encoding="utf-8")
                    expect.write_text(case["expect"], encoding="utf-8")
                    _run("compare.py", ["--expect", expect, "--actual", actual,
                                        "--out", results / "compare" / f"{case['name']}.json",
                                        "--model", model or "", "--base-url", args.base_url or "",
                                        "--api-key", args.api_key or "",
                                        "--llm-protocol", args.llm_protocol or ""], check=False)
            _write_json(results / "runs.json", runs)
            # T2B 的 baseline 不参与 T3 成本；necessity 单独从 t2b/score.json 合并
            if (results / "baseline.json").exists():
                (results / "baseline.json").unlink()
            _run("score.py", [results, "--out", results / "score.json"])
            _merge_necessity(results / "score.json", t2b_score)

            pairs = []
            for cname, ts in traces.items():
                for a, b in zip(ts, ts[1:]):
                    p = results / "idem" / f"{cname}-{a.stem}-{b.stem}.json"
                    _run("idem.py", [a, b, "--out", p], check=False)
                    rec = _read_json(p, {}) or {}
                    if "ratio" in rec:
                        pairs.append({"case": cname, "pair": f"{a.stem}->{b.stem}", **rec})
            if pairs:
                _write_json(results / "idem.json", {
                    "ratio": sum(p["ratio"] for p in pairs) / len(pairs),
                    "idempotent": all(p.get("idempotent") for p in pairs),
                    "pairs": len(pairs),
                    "idempotent_pairs": sum(1 for p in pairs if p.get("idempotent")),
                    "per_pair": pairs})
            if (results / "compare").is_dir():
                _run("compare.py", ["--aggregate", results / "compare",
                                    "--out", results / "compare.json"], check=False)
            if _t3_ready(results):
                _copy_cache(results, t3_cache,
                            ("runs.json", "idem.json", "compare.json", "golden.json", "score.json"),
                            ("traces", "idem", "compare", "actual", "expected"))
            progress.emit("stage", name="T3", status="done", cases=len(cases), runs=len(runs),
                          incomplete=not _t3_ready(results))

    if TIER_RANK[tier] == TIER_RANK["t2b"] and t2b_score:
        # T2B 只出必要性；成本属于 T3，不能因为刚跑过真实输入就冒充 #13
        s2 = _read_json(t2b_score, {}) or {}
        s2["trigger"] = (_read_json(results / "score.json", {}) or {}).get("trigger", "skipped")
        s2["cost"] = {k: "skipped" for k in ("tool_calls", "tokens", "seconds")}
        s2.pop("cost_by_case", None)
        _write_json(results / "score.json", s2)

    progress.emit("stage", name="report", status="start")
    _run("report.py", [results, "--skill", skill, "--tier", tier, "--html"]
         + (["--model", model] if model else [])
         + (["--harness", args.harness] if args.harness else [])
         + (["--evalset", evalset] if evalset else []))
    report = _read_json(results / "report.json", {}) or {}
    progress.emit("stage", name="report", status="done")
    progress.emit("done", results=str(results), report=str(results / "report.html"),
                  conclusion=report.get("conclusion"), metrics=len(report.get("metrics") or {}))
    return 0


def main():
    args = sys.argv[1:]
    class A:
        skill = None; tier = "t2a"; results = None; evalset = None; real_input = None
        model = None; backend = "auto"; progress = None; version = None
        dry_run = False; sandbox = None; harness = None
        llm_protocol = None; base_url = None; api_key = None
        rerun_t0 = False; rerun_t1 = False; rerun_t2a = False; rerun_t2b = False; rerun_t3 = False
        no_cache = False
    i = 0
    while i < len(args):
        key = args[i]
        if key in ("--skill", "--tier", "--results", "--evalset", "--real-input", "--model",
                   "--backend", "--progress", "--version", "--sandbox", "--harness",
                   "--llm-protocol", "--base-url", "--api-key"):
            if i + 1 >= len(args):
                print(f"{key} 缺值", file=sys.stderr); return 2
            setattr(A, key[2:].replace("-", "_"), args[i + 1]); i += 2
        elif key == "--dry-run":
            A.dry_run = True; i += 1
        elif key == "--rerun-t0":
            A.rerun_t0 = True; i += 1
        elif key == "--rerun-t1":
            A.rerun_t1 = True; i += 1
        elif key == "--rerun-t2a":
            A.rerun_t2a = True; i += 1
        elif key == "--rerun-t2b":
            A.rerun_t2b = True; i += 1
        elif key == "--rerun-t3":
            A.rerun_t3 = True; i += 1
        elif key == "--no-cache":
            A.no_cache = True; i += 1
        else:
            print(f"未知参数: {key}", file=sys.stderr); return 2
    if not A.skill:
        print("usage: runner.py --skill <dir> [--tier t0|t1|t2a|t2b|t3] "
              "[--evalset <dir>] [--real-input <file>] [--results <dir>] [--model m] "
              "[--backend auto|llm|pi] [--harness pi] [--progress <file>] [--rerun-t0] [--rerun-t1] [--rerun-t2a] [--rerun-t2b] [--rerun-t3] [--no-cache] [--dry-run]", file=sys.stderr)
        return 2
    try:
        return run(A)
    except Exception as exc:
        Progress(getattr(A, "progress", None)).emit("error", error=str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
