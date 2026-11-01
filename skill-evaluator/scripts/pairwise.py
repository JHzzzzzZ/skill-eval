"""无标签真实输入的 A/B 盲评：同一 prompt 的两份回答，判断哪份更好。

Seam: python pairwise.py --prompt <file> --a <file> --b <file> --out <json>
只输出偏好，不输出 accuracy；调用方应交换 a/b 再评一次，两次一致才算胜。
"""
import _console

_console.fix()

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import llm as llm_layer
from compare import extract_json_object


def build_prompt(prompt: str, a: str, b: str) -> str:
    return (
        "你是独立评审员。下面有同一个用户任务的两份回答 A 和 B，来源已隐去。\n"
        "判断哪份回答更好地完成任务：更准确、更完整、更可执行；不要因为更长或格式更多就偏袒。\n"
        "如果无法区分或各有明显缺陷，判平局。\n\n"
        f"用户任务：\n{prompt}\n\n"
        f"回答 A：\n{a}\n\n"
        f"回答 B：\n{b}\n\n"
        '只输出一个 JSON 对象：{"winner":"a|b|tie","score":0|1|2,"reason":"一句话理由"}\n'
        "score: 0=平局, 1=略好, 2=明显更好。不要输出 Markdown。"
    )


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def main() -> int:
    args = sys.argv[1:]
    prompt_file = a_file = b_file = out_path = model = base_url = api_key = protocol = None
    build_only = False
    i = 0
    while i < len(args):
        key = args[i]
        if key in ("--prompt", "--a", "--b", "--out", "--model", "--base-url",
                   "--api-key", "--llm-protocol"):
            if i + 1 >= len(args):
                print(f"{key} 缺值", file=sys.stderr)
                return 2
            val = args[i + 1]
            if key == "--prompt": prompt_file = val
            elif key == "--a": a_file = val
            elif key == "--b": b_file = val
            elif key == "--out": out_path = val
            elif key == "--model": model = val
            elif key == "--base-url": base_url = val
            elif key == "--api-key": api_key = val
            else: protocol = val
            i += 2
        elif key == "--build-only":
            build_only = True
            i += 1
        else:
            i += 1
    if not prompt_file or not a_file or not b_file:
        print("usage: pairwise.py --prompt <file> --a <file> --b <file> [--out <json>]",
              file=sys.stderr)
        return 2
    prompt = _read(prompt_file)
    text_a = _read(a_file)
    text_b = _read(b_file)
    judge_prompt = build_prompt(prompt, text_a, text_b)
    if build_only:
        print(json.dumps({"prompt": judge_prompt}, ensure_ascii=False))
        return 0

    result = {"winner": "tie", "score": 0, "reason": "未完成评审", "valid": False}
    try:
        text = llm_layer.ask(judge_prompt, model=model or None,
                             base_url_override=base_url, api_key_override=api_key,
                             protocol_override=protocol)
        payload = extract_json_object(text)
        if isinstance(payload, dict):
            winner = str(payload.get("winner") or "").lower()
            score = payload.get("score")
            if winner in ("a", "b", "tie") and isinstance(score, int) and 0 <= score <= 2:
                result = {"winner": winner, "score": score,
                          "reason": str(payload.get("reason") or "")[:500], "valid": True}
            else:
                result["reason"] = "评审输出字段不合法"
        else:
            result["reason"] = "评审输出没有 JSON"
    except Exception as exc:
        result["reason"] = f"LLM 调用失败：{exc}"[:500]
    if out_path:
        _console.write_text(out_path, json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
