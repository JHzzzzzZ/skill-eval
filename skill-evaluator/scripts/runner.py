"""确定性评估编排（ADR-0036）：T0 → T1 → T2A → T2B → T3 → report。

前端与 agent 共用本入口；阶段按 --tier 短路，进度以 NDJSON 输出（--progress 另落文件）。
不重写各脚本的测量逻辑：只负责串流程、落中间产物、报告进度。
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

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
TIERS = ("t0", "t1", "t2a", "t2b", "t3")
TIER_RANK = {name: i for i, name in enumerate(TIERS)}
EXCLUDE = {"tests", "evalsets", "uploads", "attic", ".git", "node_modules",
           "__pycache__", ".sandbox", ".skillrepos", ".pytest_cache"}
GUARD = [ROOT.parent] if (ROOT.parent / ".git").exists() else []


class RunnerError(RuntimeError):
    pass


class Progress:
    def __init__(self, path: str | None = None):
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("", encoding="utf-8")

    def emit(self, kind: str, **fields):
        event = {"type": kind, "ts": time.time(), **fields}
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


def _cases(evalset: Path) -> list:
    out = []
    for f in sorted((evalset / "cases").glob("*.json")):
        data = _read_json(f, {})
        if isinstance(data, dict) and isinstance(data.get("prompt"), str) and data["prompt"].strip():
            out.append({"name": f.stem, "prompt": data["prompt"],
                        "expect": str(data.get("expect") or "")})
    return out


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
    if args.dry_run:
        stages = [t for t in TIERS[:TIER_RANK[tier] + 1]]
        progress.emit("plan", tier=tier, stages=stages, skill=str(skill))
        return 0

    version = args.version or time.strftime("%Y%m%d-%H%M%S")
    results = Path(args.results).resolve() if args.results else \
        ROOT / "evalsets" / skill.name / "results" / f"auto-{version}"
    results.mkdir(parents=True, exist_ok=True)
    static_path = results / "static.json"
    progress.emit("stage", name="T0", status="start")

    # T0 静态检查
    _run("static_check.py", [skill, "--out", static_path])
    static = _read_json(static_path, {}) or {}
    name = static.get("name") or skill.name
    human = bool(static.get("human")) or (static.get("invoke") or {}).get("resolved") == "human"
    progress.emit("stage", name="T0", status="done", passed=static.get("passed"))

    evalset = Path(args.evalset).resolve() if args.evalset else _find_evalset(name)
    if evalset and not evalset.is_dir():
        raise RunnerError(f"评测集不存在: {evalset}")
    model = args.model or os.environ.get("SKILL_EVAL_MODEL") or None
    judge_backend = ("llm" if (args.base_url or args.api_key or args.llm_protocol) else args.backend)
    _write_json(results / "run-meta.json", {
        "skill": str(skill), "tier": tier, "evalset": str(evalset) if evalset else None,
        "model": model, "harness": args.harness, "llm_protocol": args.llm_protocol,
        "base_url": args.base_url, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })

    # T1 评审（默认 llm；未配置时 auto 回落 pi）
    if TIER_RANK[tier] >= TIER_RANK["t1"]:
        progress.emit("stage", name="T1", status="start")
        jdir = results / "judges"
        jdir.mkdir(parents=True, exist_ok=True)
        rubrics = ("brevity", "redundancy", "fallback", "precheck", "contract", "deps", "pii")
        workers = max(1, _config.get_int("run.judge_concurrency", 4))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {}
            for metric in rubrics:
                futs[pool.submit(_run, "judge_run.py",
                                 ["--metric", metric, "--skill", skill,
                                  "--out", jdir / f"{metric}.json", "--backend", judge_backend,
                                  "--raw-dir", results / "raw" / "llm-judge",
                                  "--model", model or "", "--protocol", args.llm_protocol or "",
                                  "--base-url", args.base_url or "", "--api-key", args.api_key or ""])] = metric
            for fut in as_completed(futs):
                metric = futs[fut]
                try:
                    fut.result()
                    progress.emit("judge", metric=metric, status="done")
                except Exception as exc:
                    progress.emit("judge", metric=metric, status="skipped", error=str(exc))
        progress.emit("stage", name="T1", status="done")

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
        if human:
            progress.emit("stage", name="T2A", status="skipped",
                          reason="human 型：不跑触发评测（ADR-0018）")
        elif not evalset:
            progress.emit("stage", name="T2A", status="skipped", reason="无评测集")
        else:
            groups = _prompts(evalset)
            judged = {"should": [], "should_not": [], "confusable": []}
            total = sum(len(v) for v in groups.values())
            idx = 0
            from harness import run_case  # 延迟导入：dry-run 不需要
            for key, items in groups.items():
                for item in items:
                    idx += 1
                    progress.emit("case", stage="T2A", index=idx, total=total, label=item["file"])
                    trace_path = results / "triggers" / f"{key}-{idx}.json"
                    try:
                        run_case(item["prompt"], sandbox, skill_dir=sandbox, adapter=args.harness,
                                 early_exit=True, model=model, out=trace_path)
                        jout = results / "trigger-judge" / f"{key}-{idx}.json"
                        _run("trigger_judge.py", ["--skill", sandbox / "SKILL.md",
                                                  "--trace", trace_path, "--out", jout,
                                                  "--model", model or "",
                                                  "--base-url", args.base_url or "",
                                                  "--api-key", args.api_key or "",
                                                  "--llm-protocol", args.llm_protocol or ""])
                        judged[key].append(bool((_read_json(jout, {}) or {}).get("triggered")))
                    except Exception as exc:
                        judged[key].append(False)
                        progress.emit("warn", stage="T2A", label=item["file"], error=str(exc))
            _write_json(results / "triggers.json", judged)
            _run("score.py", [results, "--out", results / "score.json"], check=False)
            progress.emit("stage", name="T2A", status="done",
                          should=sum(judged["should"]), total_should=len(judged["should"]))

    # T2B 真实场景 A/B
    t2b_score = None
    if TIER_RANK[tier] >= TIER_RANK["t2b"]:
        progress.emit("stage", name="T2B", status="start")
        if not args.real_input:
            progress.emit("stage", name="T2B", status="skipped", reason="未提供真实输入")
        else:
            from harness import run_case
            raw = Path(args.real_input).read_text(encoding="utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"prompt": raw}
            prompt = str(data.get("prompt") or "").strip()
            label = str(data.get("label") or Path(args.real_input).stem)
            if not prompt:
                raise RunnerError("真实输入缺少 prompt")
            digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
            store = ROOT / "uploads" / "real-input" / f"{digest}.json"
            _write_json(store, {"prompt": prompt, "label": label})
            _write_json(results / "real-input.meta.json", {"sha256": digest, "label": label})
            gold = prompt if not human else f"/skill:{name} {prompt}"
            t2b = results / "t2b"
            t2b.mkdir(parents=True, exist_ok=True)
            gt = t2b / "golden.json"
            bt = t2b / "baseline-trace.json"
            run_case(gold, sandbox, skill_dir=sandbox, model=model, out=gt, adapter=args.harness,
                     guard_repo=GUARD)
            run_case(prompt, sandbox, no_skill=True, model=model, out=bt, adapter=args.harness,
                     guard_repo=GUARD)
            g = _read_json(gt, {}) or {}
            b = _read_json(bt, {}) or {}
            _write_json(results / "golden.json", g)
            _write_json(t2b / "runs.json", [_metric(g, label)])
            _write_json(t2b / "baseline.json", [_metric(b, label)])
            _run("score.py", [t2b, "--out", t2b / "score.json"])
            t2b_score = t2b / "score.json"
            progress.emit("stage", name="T2B", status="done", label=label)

    # T3 核心沙箱
    if TIER_RANK[tier] >= TIER_RANK["t3"]:
        progress.emit("stage", name="T3", status="start")
        cases = _cases(evalset) if evalset else []
        if not cases:
            progress.emit("stage", name="T3", status="skipped", reason="无 cases")
        else:
            from harness import run_case
            repeat = max(1, _config.get_int("run.repeat", 3))
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
            progress.emit("stage", name="T3", status="done", cases=len(cases), runs=len(runs))

    if TIER_RANK[tier] == TIER_RANK["t2b"] and t2b_score:
        # T2B 只出必要性；成本属于 T3，不能因为刚跑过真实输入就冒充 #13
        s2 = _read_json(t2b_score, {}) or {}
        s2["trigger"] = (_read_json(results / "score.json", {}) or {}).get("trigger", "skipped")
        s2["cost"] = {k: "skipped" for k in ("tool_calls", "tokens", "seconds")}
        s2.pop("cost_by_case", None)
        _write_json(results / "score.json", s2)

    progress.emit("stage", name="report", status="start")
    _run("report.py", [results, "--skill", skill, "--tier", tier, "--html"] +
         (["--evalset", evalset] if evalset else []))
    report = _read_json(results / "report.json", {}) or {}
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
        else:
            print(f"未知参数: {key}", file=sys.stderr); return 2
    if not A.skill:
        print("usage: runner.py --skill <dir> [--tier t0|t1|t2a|t2b|t3] "
              "[--evalset <dir>] [--real-input <file>] [--results <dir>] [--model m] "
              "[--backend auto|llm|pi] [--harness pi] [--progress <file>] [--dry-run]", file=sys.stderr)
        return 2
    try:
        return run(A)
    except Exception as exc:
        Progress(getattr(A, "progress", None)).emit("error", error=str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
