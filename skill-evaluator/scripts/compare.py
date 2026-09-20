"""#9 结果可验证：expect vs actual 的 LLM 对比。Seam: python compare.py --expect <file> --actual <file>

LLM 判定用 pi CLI；--build-only 输出 prompt（离线测试），--events <jsonl> 离线解析回答。
无 JSON 回答时保守判 fail（score 0），不静默通过。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

def build_prompt(expect: str, actual: str) -> str:
    return (f"你是输出质量评审员。判断\"实际产出\"是否满足\"期望描述\"。\n"
            f"期望描述：\n{expect}\n\n实际产出：\n{actual}\n\n"
            '只回答一个 JSON 对象：{"match": true/false, "score": 0|1|2, "reason": "一句话理由"}\n'
            "score: 0=不匹配, 1=部分匹配, 2=完全匹配。")


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
                            m = re.search(r"\{[^{}]*\}", c.get("text", ""), re.DOTALL)
                            if m:
                                try:
                                    return json.loads(m.group(0))
                                except json.JSONDecodeError:
                                    pass
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
            Path(out_path).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
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
        cmd = ["pi", "-p", "--no-session", "--no-skills"]
        eff_model = model or os.environ.get("SKILL_EVAL_MODEL")
        if eff_model:
            cmd += ["--model", eff_model]
        if thinking:
            cmd += ["--thinking", thinking]
        cmd += ["--", prompt]
        resolved = shutil.which("pi") or shutil.which("pi.cmd") or shutil.which("pi.exe")
        if resolved:
            cmd[0] = resolved
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=300)
            answer = extract_answer(proc.stdout)
        except FileNotFoundError:
            answer = {"match": False, "score": 0, "reason": "pi CLI 不存在"}
        except subprocess.TimeoutExpired:
            answer = {"match": False, "score": 0, "reason": "对比超时（300s）"}
    if not isinstance(answer.get("match"), bool):
        answer = {"match": False, "score": 0, "reason": f"LLM 回答无合法 match 字段: {str(answer)[:200]}"}
    if out_path:
        Path(out_path).write_text(json.dumps(answer, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(answer, ensure_ascii=False))


if __name__ == "__main__":
    main()
