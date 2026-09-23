"""#9 结果可验证：expect vs actual 的 LLM 对比。Seam: python compare.py --expect <file> --actual <file>

LLM 判定用 pi CLI（--mode json，prompt 走 stdin，见 _subprocess.ask_json）；
--build-only 输出 prompt（离线测试），--events <jsonl> 离线解析回答。
无 JSON 回答时保守判 fail（score 0），不静默通过。
"""
import _console

_console.fix()

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _subprocess import ask_json, build_mode_args, kill_tree, run_pi  # noqa: F401  (kill_tree/run_pi 供其他脚本复用)

def build_prompt(expect: str, actual: str) -> str:
    return (f"你是输出质量评审员。判断\"实际产出\"是否满足\"期望描述\"。\n"
            f"期望描述：\n{expect}\n\n实际产出：\n{actual}\n\n"
            '只回答一个 JSON 对象：{"match": true/false, "score": 0|1|2, "reason": "一句话理由"}\n'
            "score: 0=不匹配, 1=部分匹配, 2=完全匹配。")


def extract_json_object(text: str):
    """提取文本中第一个花括号配对完整的 JSON 对象（支持嵌套 items[]/字符串内花括号）。

    旧 regex 版 `\{[^{}]*\}` 只能吃扁平对象：嵌套输出会先命中内层对象
    （trigger_judge 的 items[] 契约被截成单个 item → 缺 triggered → 静默保守判未触发）。
    """
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break  # 这个起点配不成合法 JSON，换下一个 {
        start = text.find("{", start + 1)
    return None


def extract_answer(events_text: str) -> dict:
    """从 pi --mode json 事件流提取最后一个 assistant 文本里的 JSON 裁决。"""
    try:
        default = {"match": False, "score": 0, "reason": "无法从回答中解析 JSON 裁决"}
        texts = []
        for line in events_text.splitlines():
            line = line.strip()
            if not line:
                continue
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
                            obj = extract_json_object(c.get("text", ""))
                            if isinstance(obj, dict):
                                return obj
        return default
    except Exception:
        return {"match": False, "score": 0, "reason": "回答解析异常"}


def main():
    args = sys.argv[1:]
    expect_file = actual_file = model = thinking = events_file = out_path = None
    build_only = False
    i = 0
    while i < len(args):
        if args[i] == "--expect":
            expect_file = args[i + 1]; i += 2
        elif args[i] == "--actual":
            actual_file = args[i + 1]; i += 2
        elif args[i] == "--model":
            model = args[i + 1]; i += 2
        elif args[i] == "--thinking":
            thinking = args[i + 1]; i += 2
        elif args[i] == "--events":
            events_file = args[i + 1]; i += 2
        elif args[i] == "--out":
            out_path = args[i + 1]; i += 2
        elif args[i] == "--build-only":
            build_only = True; i += 1
        else:
            i += 1
    if "--aggregate" in args:
        ai = args.index("--aggregate")
        cdir = Path(args[ai + 1]) if ai + 1 < len(args) else None
        if cdir is None or not cdir.is_dir():
            print(f"aggregate 目录不存在: {cdir}", file=sys.stderr)
            sys.exit(2)
        per = []
        for f in sorted(cdir.glob("*.json")):
            try:
                j = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(j.get("score"), int):
                    per.append({"name": f.stem, **j})
            except (OSError, json.JSONDecodeError):
                continue
        if not per:
            out = {"mean_score": None, "match": None, "per_case": [],
                   "note": "无有效裁决文件"}
        else:
            mean = sum(p["score"] for p in per) / len(per)
            matched = sum(1 for p in per if p.get("match"))
            out = {"mean_score": mean, "match": matched * 2 > len(per),
                   "per_case": per, "note": f"{matched}/{len(per)} case 匹配"}
        if out_path:
            _console.write_text(out_path, json.dumps(out, ensure_ascii=False, indent=2))
        print(json.dumps(out, ensure_ascii=False))
        return

    if not expect_file or not actual_file:
        print("usage: compare.py --expect <file> --actual <file> [--aggregate <dir>]", file=sys.stderr)
        sys.exit(2)
    try:
        expect = Path(expect_file).read_text(encoding="utf-8", errors="replace")
        actual = Path(actual_file).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"read fail: {e}", file=sys.stderr)
        sys.exit(2)
    prompt = build_prompt(expect, actual)

    if build_only:
        print(json.dumps({"prompt": prompt}, ensure_ascii=False))
        return

    if events_file:
        answer = extract_answer(Path(events_file).read_text(encoding="utf-8", errors="replace"))
    else:
        mode_args = build_mode_args(model or os.environ.get("SKILL_EVAL_MODEL"), thinking)
        try:
            events_text = ask_json(mode_args, prompt, timeout=300)
            answer = extract_answer(events_text)
        except FileNotFoundError:
            answer = {"match": False, "score": 0, "reason": "pi CLI 不存在"}
        except subprocess.TimeoutExpired:
            answer = {"match": False, "score": 0, "reason": "对比超时（300s）"}
    if not isinstance(answer.get("match"), bool):
        answer = {"match": False, "score": 0, "reason": f"LLM 回答无合法 match 字段: {str(answer)[:200]}"}
    if out_path:
        _console.write_text(out_path, json.dumps(answer, ensure_ascii=False))
    print(json.dumps(answer, ensure_ascii=False))


if __name__ == "__main__":
    main()
