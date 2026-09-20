"""测试基建约定（Windows 必读）。

seam 脚本用 `print()` 输出中文 JSON（ensure_ascii=False）。在 GBK locale 的
Windows 控制台下，子进程 stdout 默认按 locale 编码——写 UTF-8 中文会触发
UnicodeDecodeError（pytest 的 reader 线程炸掉 → stdout=None → json.loads(None)
TypeError）。46 个用例集体变红的根因就是这个，不是产品代码回归。

本文件在 pytest 收集前把 PYTHONIOENCODING 设为 utf-8：所有测试拉起的子进程
（seam 脚本）stdout/stderr 都走 UTF-8。父进程侧的解码由各测试文件的
subprocess.run(..., encoding="utf-8") 承担（两侧缺一不可）。

约定：测试里新加 subprocess 调用必须带 encoding="utf-8"；中间产物数据源用
--out 文件（UTF-8），stdout 只当回显。
"""
import os

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
