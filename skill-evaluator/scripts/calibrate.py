"""Judge 校准集跑分：同一 case 多次评审，算逐项一致率与标注准确率。

Seam: python scripts/calibrate.py --metric brevity [--cases calibration/brevity]
      [--samples 3] [--backend llm] [--out calibration/results/brevity.json]

校准集目录：
  calibration/<metric>/<case>/skill/...      被测 skill（SKILL.md 等）
  calibration/<metric>/<case>/expected.json  {"items": {"检查点名": true/false}}

只做统计，不改 rubric；需要真实 LLM 配置。一致率低于 --min-agreement 时退出码 1。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import _console

_console.fix()

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
JUDGE = SCRIPTS / "judge_run.py"
DEFAULT_CASES = ROOT / "calibration"


def _majority(values: list) -> tuple:
    valid = [v for v in values if isinstance(v, bool)]
    if not valid:
        return None, 0.0
    yes = sum(1 for v in valid if v)
    no = len(valid) - yes
    return yes * 2 > len(valid), max(yes, no) / len(valid)


def summarize(case_results: list, samples: int) -> dict:
    """case_results: [{"expected": {name: bool}, "observations": {name: [bool|None]}}]"""
    item_agreement: dict[str, list] = {}
    item_accuracy: dict[str, list] = {}
    item_valid: dict[str, int] = {}
    for case in case_results:
        expected = case.get("expected", {})
        observations = case.get("observations", {})
        for name, want in expected.items():
            got, agreement = _majority(observations.get(name, []))
            item_agreement.setdefault(name, []).append(agreement)
            item_accuracy.setdefault(name, []).append(got == bool(want) if got is not None else False)
            item_valid[name] = item_valid.get(name, 0) + sum(
                1 for v in observations.get(name, []) if isinstance(v, bool))
    items = {}
    for name, values in item_agreement.items():
        items[name] = {
            "agreement": round(sum(values) / len(values), 3) if values else 0.0,
            "accuracy": round(sum(item_accuracy.get(name, [])) / len(item_accuracy.get(name, [])), 3)
            if item_accuracy.get(name) else 0.0,
            "valid": item_valid.get(name, 0),
            "cases": len(values),
            "samples": samples,
        }
    overall_agreement = (sum(v["agreement"] for v in items.values()) / len(items)
                         if items else 0.0)
    overall_accuracy = (sum(v["accuracy"] for v in items.values()) / len(items)
                        if items else 0.0)
    return {"items": items,
            "overall_agreement": round(overall_agreement, 3),
            "overall_accuracy": round(overall_accuracy, 3)}


def _run_judge(metric: str, skill: Path, args, out: Path) -> dict:
    cmd = [sys.executable, str(JUDGE), "--metric", metric, "--skill", str(skill),
           "--out", str(out), "--backend", args.backend]
    for key, flag in (("model", "--model"), ("base_url", "--base-url"),
                      ("api_key", "--api-key"), ("protocol", "--protocol")):
        value = getattr(args, key)
        if value:
            cmd += [flag, str(value)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=args.timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"judge_run 失败（{proc.returncode}）：{(proc.stderr or proc.stdout)[-500:]}")
    return json.loads(out.read_text(encoding="utf-8"))


def main() -> int:
    args = sys.argv[1:]
    class A:
        metric = None; cases = None; samples = 3; backend = "auto"
        model = None; base_url = None; api_key = None; protocol = None
        out = None; min_agreement = 0.8; min_accuracy = 0.9; timeout = 300
    i = 0
    while i < len(args):
        key = args[i]
        if key in ("--metric", "--cases", "--samples", "--backend", "--model",
                   "--base-url", "--api-key", "--protocol", "--out", "--min-agreement",
                   "--min-accuracy", "--timeout"):
            if i + 1 >= len(args):
                print(f"{key} 缺值", file=sys.stderr); return 2
            name = key[2:].replace("-", "_")
            setattr(A, name, args[i + 1]); i += 2
        else:
            print(f"未知参数: {key}", file=sys.stderr); return 2
    if not A.metric:
        print("usage: calibrate.py --metric <name> [--cases <dir>] [--samples 3] "
              "[--backend auto|llm|pi] [--out <json>]", file=sys.stderr)
        return 2
    A.samples = max(1, int(A.samples))
    A.min_agreement = float(A.min_agreement)
    A.min_accuracy = float(A.min_accuracy)
    A.timeout = int(A.timeout)
    base = Path(A.cases) if A.cases else DEFAULT_CASES / A.metric
    if not base.is_dir():
        print(f"校准集不存在: {base}", file=sys.stderr)
        return 2
    cases = sorted(p.parent for p in base.glob("*/expected.json"))
    if not cases:
        print(f"没有 case（需要 <case>/skill/SKILL.md + expected.json）: {base}", file=sys.stderr)
        return 2

    case_results = []
    errors = []
    for case in cases:
        skill = case / "skill"
        expected = json.loads((case / "expected.json").read_text(encoding="utf-8")).get("items", {})
        observations = {name: [] for name in expected}
        for _ in range(A.samples):
            try:
                with tempfile.TemporaryDirectory() as td:
                    result = _run_judge(A.metric, skill, A, Path(td) / "judge.json")
                got = {item.get("name"): item.get("pass") for item in result.get("items", [])}
                for name in expected:
                    observations[name].append(got.get(name))
            except Exception as exc:
                errors.append({"case": case.name, "error": str(exc)})
                for name in expected:
                    observations[name].append(None)
        case_results.append({"expected": expected, "observations": observations})

    summary = summarize(case_results, A.samples)
    summary.update({"metric": A.metric, "cases_dir": str(base),
                    "case_count": len(cases), "samples": A.samples,
                    "min_agreement": A.min_agreement, "min_accuracy": A.min_accuracy,
                    "backend": A.backend, "model": A.model or "",
                    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "errors": errors})
    failed = [name for name, item in summary["items"].items()
              if item["agreement"] < A.min_agreement]
    failed_acc = [name for name, item in summary["items"].items()
                  if item["accuracy"] < A.min_accuracy]
    summary["failed_items"] = failed
    summary["failed_accuracy_items"] = failed_acc
    summary["passed"] = not failed and not failed_acc and not errors

    out = Path(A.out) if A.out else ROOT / "calibration" / "results" / f"{A.metric}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"校准 {A.metric}: {len(cases)} cases × {A.samples} samples")
    for name, item in summary["items"].items():
        flag = "✓" if item["agreement"] >= A.min_agreement else "✗"
        print(f"  {flag} {name}: agreement={item['agreement']:.2f} "
              f"accuracy={item['accuracy']:.2f} valid={item['valid']}/{A.samples}")
    print(f"  overall agreement={summary['overall_agreement']:.2f} "
          f"accuracy={summary['overall_accuracy']:.2f} "
          f"(thresholds {A.min_agreement:.2f}/{A.min_accuracy:.2f})")
    if errors:
        print(f"  errors={len(errors)}（首条：{errors[0]['error']}）")
    print(f"  -> {out}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
