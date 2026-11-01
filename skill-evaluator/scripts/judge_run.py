"""T1 评审执行器：rubric + 被测包 → 契约 JSON（ADR-0034）。

默认走 llm 接口层（OpenAI-compatible / Anthropic），不经过 pi；--backend pi 保留旧路径。
固定单发、不带 tools；输出经 judge_runner.validate 校验后才落盘。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import _console

_console.fix()

import _config
import _subprocess
import judge_runner
import llm as llm_layer

SCRIPTS = Path(__file__).resolve().parent
EVALUATOR_ROOT = SCRIPTS.parent
JUDGES = EVALUATOR_ROOT / "judges"
def default_max_chars() -> int:
    return _config.get_int("llm.max_input_chars", 60000)
EXCLUDE_DIRS = {"tests", "evalsets", "uploads", "attic", ".git", "node_modules",
                "__pycache__", ".sandbox", ".skillrepos", ".pytest_cache", "runs", "cache", "calibration"}
SUFFIXES = {".md", ".py", ".sh", ".ps1", ".cmd", ".bat", ".txt", ".json", ".yaml", ".yml", ".toml"}
RUBRICS = ("brevity", "redundancy", "fallback", "precheck", "contract", "deps", "pii")


def collect_files(skill_dir: Path, max_chars: int | None = None) -> list:
    """收集被测包文本；SKILL.md 无条件置首，其余文件按预算追加。

    旧实现先遍历、后排序，大文件会先吃满 max_chars，导致 SKILL.md 被截掉——
    判官只能看到附属文件，误报“SKILL.md 缺失”。SKILL.md 是评审主体，必须先进预算。
    """
    max_chars = default_max_chars() if max_chars is None else max_chars
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise ValueError(f"缺 SKILL.md：{skill_dir}")
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValueError(f"读不到 SKILL.md：{exc}") from exc
    files = [{"path": "SKILL.md", "text": text[:max_chars]}]
    total = len(files[0]["text"])
    for p in sorted(skill_dir.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in SUFFIXES:
            continue
        if p.resolve() == skill_md.resolve():
            continue
        rel = p.relative_to(skill_dir).as_posix()
        if any(part in EXCLUDE_DIRS for part in Path(rel).parts):
            continue
        if total >= max_chars:
            break
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        left = max(0, max_chars - total)
        if len(text) > left:
            text = text[:left] + "\n…（截断：达到评审输入上限）"
        files.append({"path": rel, "text": text})
        total += len(text)
    return files


def build_prompt(rubric: str, files: list) -> str:
    parts = [rubric.rstrip(), "", "## 被测文件"]
    for f in files:
        parts += [f"### {f['path']}", "```", f["text"].rstrip(), "```", ""]
    parts.append("只输出 rubric 要求的 JSON，不要输出解释或额外文本。")
    return "\n".join(parts)


def _pi_text(events_text: str) -> str:
    """从 pi --mode json 事件流取最后一条 assistant 文本。"""
    texts = []
    for line in events_text.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") != "agent_end":
            continue
        for msg in reversed(ev.get("messages", [])):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                for c in msg.get("content", []):
                    if isinstance(c, dict) and c.get("type") == "text":
                        texts.append(c.get("text", ""))
                if texts:
                    return "\n".join(reversed(texts))
    return ""


def ask(prompt: str, backend: str, model: str | None, base_url: str | None,
        api_key: str | None, protocol: str | None = None) -> tuple[str, str]:
    if backend == "auto":
        backend = "llm" if llm_layer.configured() else "pi"
    if backend == "llm":
        text = llm_layer.ask(prompt, model=model, base_url_override=base_url,
                             api_key_override=api_key, protocol_override=protocol)
        return text, "llm"
    events = _subprocess.ask_json(_subprocess.build_mode_args(
        model or _config.get_str("llm.model", "") or None), prompt)
    return _pi_text(events), "pi"


def run(metric: str, skill_dir: Path, out: Path, *, model=None, backend="auto",
        base_url=None, api_key=None, protocol=None, max_chars: int | None = None,
        raw_dir: Path | None = None) -> dict:
    rubric_path = JUDGES / f"{metric}.md"
    if not rubric_path.is_file():
        raise ValueError(f"rubric 不存在: {rubric_path}")
    rubric = rubric_path.read_text(encoding="utf-8")
    files = collect_files(skill_dir, max_chars)
    prompt = build_prompt(rubric, files)
    text, used = ask(prompt, backend, model, base_url, api_key, protocol)
    payload = llm_layer.extract_json(text)
    errors = judge_runner.validate(payload) if payload is not None else ["模型输出里没有 JSON 对象"]
    if errors:
        retry = prompt + "\n\n上一次输出不合法：\n- " + "\n- ".join(errors) + "\n请只输出修正后的 JSON。"
        text2, used = ask(retry, backend, model, base_url, api_key, protocol)
        payload = llm_layer.extract_json(text2)
        errors = judge_runner.validate(payload) if payload is not None else ["模型输出里没有 JSON 对象"]
        text = text2
    if errors:
        raise ValueError("评审输出不满足契约：" + "；".join(errors))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if raw_dir:
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / f"{metric}.json").write_text(json.dumps(
            {"metric": metric, "backend": used, "model": model,
             "prompt": prompt, "response": text, "payload": payload},
            ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main():
    args = sys.argv[1:]
    metric = skill = out = model = backend = base_url = api_key = raw_dir = protocol = None
    max_chars = None
    i = 0
    while i < len(args):
        if args[i] in ("--metric", "--skill", "--out", "--model", "--backend",
                       "--base-url", "--api-key", "--raw-dir", "--protocol"):
            key = args[i]
            val = args[i + 1]; i += 2
            if key == "--metric": metric = val
            elif key == "--skill": skill = val
            elif key == "--out": out = val
            elif key == "--model": model = val
            elif key == "--backend": backend = val
            elif key == "--base-url": base_url = val
            elif key == "--api-key": api_key = val
            elif key == "--raw-dir": raw_dir = val
            elif key == "--protocol": protocol = val
        elif args[i] == "--max-chars":
            max_chars = int(args[i + 1]); i += 2
        else:
            i += 1
    if not metric or not skill or not out:
        print("usage: judge_run.py --metric <name> --skill <dir> --out <file> "
              "[--model m] [--backend auto|llm|pi] [--base-url u] [--api-key k] [--raw-dir d]",
              file=sys.stderr)
        sys.exit(2)
    if metric not in RUBRICS:
        print(f"unknown metric: {metric}（可选 {', '.join(RUBRICS)}）", file=sys.stderr)
        sys.exit(2)
    try:
        payload = run(metric, Path(skill), Path(out), model=model, backend=backend or "auto",
                      base_url=base_url, api_key=api_key, protocol=protocol,
                      max_chars=max_chars, raw_dir=Path(raw_dir) if raw_dir else None)
    except Exception as exc:
        print(json.dumps({"ok": False, "metric": metric, "error": str(exc)}, ensure_ascii=False))
        sys.exit(2)
    print(json.dumps({"ok": True, "metric": metric, "score": payload.get("score"),
                      "items": len(payload.get("items") or [])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
