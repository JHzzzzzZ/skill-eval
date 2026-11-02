"""单指标重跑（只 T1 文档评审）：只替换目标 rubric，其他裁决原样保留。

Seam: python scripts/rerun_metrics.py --results <results> --metric "#2" [--skill <dir>]
      [--model m] [--backend auto|llm|pi] [--base-url u] [--api-key k] [--protocol p]

为什么单独一个入口：runner 的 T1 检查点是整阶段的，重跑时会清空 judges/；这里用
“临时文件 → 原子替换”只改一个 rubric，再让 report.py 读全部 7 份重出报告。
不写阶段缓存，不动其他 rubric/trace/score。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import _console

_console.fix()

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
JUDGES = ROOT / "judges"

# 报告指标 → T1 rubric。一个 rubric 可能服务多个指标（#8/#9 共用 contract）。
METRIC_RUBRIC = {
    "#2": "brevity",
    "#4": "pii",
    "#5": "redundancy",
    "#6": "fallback",
    "#7": "precheck",
    "#8": "contract",
    "#9": "contract",
    "#12": "deps",
}
RUBRICS = ("brevity", "redundancy", "fallback", "precheck", "contract", "deps", "pii")


def normalize_metric(value: str) -> str:
    text = str(value or "").strip()
    if not text.startswith("#"):
        text = "#" + text
    if text not in METRIC_RUBRIC:
        raise ValueError(f"{value!r} 不是 T1 可单跑指标；可选：{', '.join(METRIC_RUBRIC)}")
    return text


def _read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _judge_cmd(*, rubric: str, skill: Path, out: Path, model, backend, base_url,
               api_key, protocol, raw_dir: Path) -> list:
    cmd = [sys.executable, str(SCRIPTS / "judge_run.py"),
           "--metric", rubric, "--skill", str(skill), "--out", str(out),
           "--backend", backend or "auto"]
    if model:
        cmd += ["--model", str(model)]
    if base_url:
        cmd += ["--base-url", str(base_url)]
    if api_key:
        cmd += ["--api-key", str(api_key)]
    if protocol:
        cmd += ["--protocol", str(protocol)]
    cmd += ["--raw-dir", str(raw_dir)]
    return cmd


def rerun_metric(results, metric: str, *, skill=None, model=None, backend="auto",
                 base_url=None, api_key=None, protocol=None,
                 judge_fn=None, report_fn=None) -> dict:
    """重跑一个 T1 rubric 并重出报告；失败时目标 judge 保持原样。"""
    results = Path(results)
    metric = normalize_metric(metric)
    rubric = METRIC_RUBRIC[metric]
    jdir = results / "judges"
    target = jdir / f"{rubric}.json"
    if not target.is_file():
        raise ValueError(f"目标 rubric 不存在：{target}")
    missing = [name for name in RUBRICS if not (jdir / f"{name}.json").is_file()]
    if missing:
        raise ValueError("T1 检查点不完整，不能单指标重跑；缺：" + "、".join(missing))
    meta = _read_json(results / "run-meta.json", {}) or {}
    skill_dir = Path(skill or meta.get("skill") or "")
    if not (skill_dir / "SKILL.md").is_file():
        raise ValueError(f"找不到被测 skill：{skill_dir}")
    tmp = jdir / f".rerun-{rubric}.json"
    raw_dir = results / "raw" / "llm-judge"
    cmd = _judge_cmd(rubric=rubric, skill=skill_dir, out=tmp, model=model,
                     backend=backend, base_url=base_url, api_key=api_key,
                     protocol=protocol, raw_dir=raw_dir)
    try:
        if judge_fn is None:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
            if proc.returncode != 0:
                raise RuntimeError((proc.stdout or proc.stderr or "judge_run 失败")[-800:])
        else:
            judge_fn(cmd)
        if not tmp.is_file() or not isinstance(_read_json(tmp, None), dict):
            raise RuntimeError("重跑没有产出合法 judge JSON；原 rubric 未改动")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)

    # 只做报告重算，不再跑 runner/T1/T2；report.py 读 7 份现有 judge。
    report_cmd = [sys.executable, str(SCRIPTS / "report.py"), str(results),
                  "--skill", str(skill_dir), "--tier", str(meta.get("tier") or "t1"), "--html"]
    if meta.get("evalset"):
        report_cmd += ["--evalset", str(meta["evalset"])]
    if model or meta.get("model"):
        report_cmd += ["--model", str(model or meta.get("model"))]
    if meta.get("harness"):
        report_cmd += ["--harness", str(meta["harness"])]
    if report_fn is None:
        proc = subprocess.run(report_cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            raise RuntimeError((proc.stdout or proc.stderr or "report 失败")[-800:])
    else:
        report_fn(report_cmd)
    record = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "metric": metric,
              "rubric": rubric, "skill": str(skill_dir)}
    with (results / "rerun-metrics.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"ok": True, "metric": metric, "rubric": rubric,
            "report": str(results / "report.html"), "record": record}


def _cli():
    args = sys.argv[1:]
    values = {"results": None, "metric": None, "skill": None, "model": None,
              "backend": "auto", "base_url": None, "api_key": None, "protocol": None}
    i = 0
    while i < len(args):
        if args[i] in ("--results", "--metric", "--skill", "--model", "--backend",
                       "--base-url", "--api-key", "--protocol") and i + 1 < len(args):
            values[args[i][2:].replace("-", "_")] = args[i + 1]; i += 2
        else:
            i += 1
    if not values["results"] or not values["metric"]:
        print("usage: rerun_metrics.py --results <dir> --metric '#2' [--skill <dir>] "
              "[--model m] [--backend auto|llm|pi] [--base-url u] [--api-key k] [--protocol p]",
              file=sys.stderr)
        return 2
    try:
        out = rerun_metric(values["results"], values["metric"], skill=values["skill"],
                           model=values["model"], backend=values["backend"],
                           base_url=values["base_url"], api_key=values["api_key"],
                           protocol=values["protocol"])
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
