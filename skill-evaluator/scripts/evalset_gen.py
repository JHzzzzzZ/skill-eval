"""生成评测集草稿（T2A 前置）：LLM 读 SKILL.md → should/should-not/confusable/cases。

只写草稿，不写 meta.json（未冻结）；条数/质量仍由 evalset_count.py / evalset_check.py 把关。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import _console

_console.fix()

import _config
import judge_run
import llm as llm_layer

SCRIPTS = Path(__file__).resolve().parent


def build_prompt(skill_text: str, count: int) -> str:
    return f"""你是 skill 评测集生成器。阅读下面的 SKILL.md，生成触发集与真实用例。

要求：
- should：{count} 条“应该触发这个 skill”的用户请求；不要照抄 description 的措辞，要像真实用户会说的话。
- should_not：{count} 条“不该触发”的请求，主题相近但应由别的 skill/通用能力处理。
- confusable：{count} 条容易误判的边界请求（可能该触发，也可能不该；用于测 precision）。
- cases：{count} 条真实任务，每条含 prompt 与可验证的 expect（期望产物/行为描述）。

只输出一个 JSON 对象；不要 Markdown 代码围栏、不要解释、不要输出数组：
{{"should":[{{"prompt":"..."}}],"should_not":[{{"prompt":"..."}}],
 "confusable":[{{"prompt":"..."}}],"cases":[{{"prompt":"...","expect":"..."}}]}}

SKILL.md：
{skill_text[:60000]}
"""


def _items(value, key="prompt"):
    out = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append({key: item.strip()})
            elif isinstance(item, dict) and isinstance(item.get(key), str) and item[key].strip():
                out.append({key: item[key].strip()})
    return out


def generate(skill_dir: Path, out: Path, count: int, *, model=None, backend="auto",
             base_url=None, api_key=None, protocol=None) -> dict:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise ValueError(f"缺 SKILL.md: {skill_dir}")
    out.mkdir(parents=True, exist_ok=True)
    raw_dir = out / "raw" / "evalset-gen"
    raw_dir.mkdir(parents=True, exist_ok=True)
    prompt = build_prompt(skill_md.read_text(encoding="utf-8"), count)
    max_tokens = max(4096, min(16384, count * 800))
    text, _used = judge_run.ask(prompt, backend, model, base_url, api_key, protocol,
                                max_tokens=max_tokens)
    (raw_dir / "response-1.txt").write_text(text, encoding="utf-8")
    payload = llm_layer.extract_json(text)
    if not isinstance(payload, dict):
        retry = (prompt + "\n\n上一次输出不是合法 JSON 对象（可能是 Markdown 围栏、解释文字或被截断）。"
                 "这次只输出一个完整 JSON 对象，不要任何其他内容。")
        text2, _used = judge_run.ask(retry, backend, model, base_url, api_key, protocol,
                                     max_tokens=max_tokens)
        (raw_dir / "response-2.txt").write_text(text2, encoding="utf-8")
        payload = llm_layer.extract_json(text2)
    if not isinstance(payload, dict):
        raise ValueError(f"模型输出不是 JSON 对象（原始输出已存 {raw_dir}）")
    groups = {k: _items(payload.get(k)) for k in ("should", "should_not", "confusable")}
    cases = _items(payload.get("cases"), "prompt")
    cases = [c for c in cases if isinstance(c.get("expect"), str) and c["expect"].strip()]
    if not groups["should"]:
        raise ValueError("生成结果没有 should 项")
    for group, sub in (("should", "should"), ("should_not", "should-not"),
                       ("confusable", "confusable")):
        d = out / "triggers" / sub
        d.mkdir(parents=True, exist_ok=True)
        for i, item in enumerate(groups[group], 1):
            (d / f"s{i}.json").write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
    cd = out / "cases"
    cd.mkdir(parents=True, exist_ok=True)
    for i, item in enumerate(cases, 1):
        (cd / f"c{i}.json").write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "DRAFT-README.json").write_text(json.dumps(
        {"note": "draft - NOT frozen, pending review"}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"out": str(out), "counts": {k: len(v) for k, v in groups.items()},
            "cases": len(cases)}


def main():
    args = sys.argv[1:]
    skill = out = model = backend = base_url = api_key = protocol = None
    count = 10
    i = 0
    while i < len(args):
        key = args[i]
        if key in ("--skill", "--out", "--model", "--backend", "--base-url",
                   "--api-key", "--llm-protocol"):
            val = args[i + 1]; i += 2
            if key == "--skill": skill = val
            elif key == "--out": out = val
            elif key == "--model": model = val
            elif key == "--backend": backend = val
            elif key == "--base-url": base_url = val
            elif key == "--api-key": api_key = val
            elif key == "--llm-protocol": protocol = val
        elif key == "--count":
            count = int(args[i + 1]); i += 2
        else:
            i += 1
    if not skill or not out:
        print("usage: evalset_gen.py --skill <dir> --out <evalset dir> [--count 10] "
              "[--backend auto|llm|pi] [--model m]", file=sys.stderr)
        sys.exit(2)
    try:
        result = generate(Path(skill), Path(out), count, model=model, backend=backend or "auto",
                          base_url=base_url, api_key=api_key, protocol=protocol)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        sys.exit(2)
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
