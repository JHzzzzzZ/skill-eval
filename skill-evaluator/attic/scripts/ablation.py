"""#6 消融运行数据准备：按 ## 标题切 SKILL.md，删指定段，其余文件原样复制。
Seam: python ablation.py --skill <dir> --delete "标题1,标题2" --out <dir> | --list
"""
import _console

_console.fix()

import json
import shutil
import sys
from pathlib import Path


def split_sections(text: str):
    """按 ## 标题切段。返回 [(标题, 段落全文)]；frontmatter+首个 ## 前的内容标题为 ''。"""
    lines = text.splitlines(keepends=True)
    sections, current, current_title = [], [], None
    for line in lines:
        if line.startswith("## "):
            if current is not None:
                sections.append((current_title, "".join(current)))
            current_title, current = line[3:].strip(), [line]
        else:
            if current is None:
                current, current_title = [], "" if line.startswith("---") else None
            current.append(line)
    if current is not None:
        sections.append((current_title, "".join(current)))
    return sections


def main():
    args = sys.argv[1:]
    skill_dir = out_dir = None
    delete = []
    list_only = False
    i = 0
    while i < len(args):
        if args[i] == "--skill":
            skill_dir = Path(args[i + 1]); i += 2
        elif args[i] == "--out":
            out_dir = Path(args[i + 1]); i += 2
        elif args[i] == "--delete":
            delete = [s.strip() for s in args[i + 1].split(",") if s.strip()]; i += 2
        elif args[i] == "--list":
            list_only = True; i += 1
        else:
            i += 1
    if not skill_dir:
        print("usage: ablation.py --skill <dir> [--delete t1,t2 | --list] --out <dir>", file=sys.stderr)
        sys.exit(2)
    if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").is_file():
        print(f"skill 目录或 SKILL.md 不存在: {skill_dir}", file=sys.stderr)
        sys.exit(2)
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    sections = split_sections(text)
    titles = [t for t, _ in sections if t]

    if list_only:
        print(json.dumps({"sections": [f"## {t}" for t in titles]}, ensure_ascii=False))
        return

    unknown = [d for d in delete if d not in titles]
    if unknown:
        print(f"unknown sections: {unknown}; available: {titles}", file=sys.stderr)
        sys.exit(2)

    if not out_dir:
        print("--out required with --delete", file=sys.stderr)
        sys.exit(2)
    out_dir.mkdir(parents=True, exist_ok=True)
    new_text = "".join(body for t, body in sections if t not in delete)
    _console.write_text(out_dir / "SKILL.md", new_text)
    for f in skill_dir.iterdir():
        if f.name == "SKILL.md":
            continue
        if f.is_dir():
            shutil.copytree(f, out_dir / f.name, dirs_exist_ok=True)  # 子目录资产不能静默丢弃
        elif f.is_file():
            shutil.copy2(f, out_dir / f.name)
    print(json.dumps({"sections": titles, "deleted": delete, "out": str(out_dir)},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
