"""控制台编码守卫（Windows GBK 控制台实锤缺陷）。

脚本的权威输出是 `--out` 落盘文件（UTF-8，契约见 reference.md § 中间产物契约）；
stdout 只是同步回显。GBK 控制台下 print 中文/特殊符号（如 ⇒）会抛
UnicodeEncodeError：产物已落盘但进程以非零退出/ traceback 告终，调用方误判失败。

fix() 把 stdout/stderr 的不可编码字符降级为替换符（U+FFFD）：回显可能缺字，
但不崩、不污染退出码。全部脚本入口处调用。
"""
import sys
from pathlib import Path


def fix() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass  # 流已被包装/重定向（pytest capsys 等）时静默放过


def write_text(path, text: str) -> None:
    """落盘权威产物：先建父目录再写（统一入口）。

    `--out` 指到还不存在的目录时，Path.write_text 直接 FileNotFoundError（v2 快照首跑踩到），
    而流程文档并没要求先 mkdir。所有脚本的 --out 落盘都走这里。
    """
    p = Path(path)
    parent = p.parent
    if str(parent) not in ("", ".") and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
