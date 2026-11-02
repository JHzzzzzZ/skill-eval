"""执行适配器契约（阶段 A；设计见 docs/design/execution-adapter.md）。

core（trace_run）只认规范化事件：`step` / `tool_error` / `usage` / `answer` /
`skill_loaded` / `adapter_error`。adapter 负责命令构造、事件翻译、skill 加载判定；
步数/token 上限、超时、杀进程树、early-exit 停止与 trace schema 留在 core。

本文件不读写业务文件、不读 config：import 除控制台编码守卫外无副作用。
"""
from __future__ import annotations

import _console

_console.fix()

import posixpath
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

# 规范化事件类型（core 只认这几种；未知类型直接忽略）
STEP = "step"
TOOL_ERROR = "tool_error"
USAGE = "usage"
ANSWER = "answer"
SKILL_LOADED = "skill_loaded"
ADAPTER_ERROR = "adapter_error"

# 读取类动词（ADR-0007 修订二）：token 只报文件名时，要求同一条命令里有读取动词才算加载；
# `find . -name SKILL.md` 这类只列名字、不读内容的调用不算——旧口径会在这一步误杀 should-not。
READ_VERBS = re.compile(r"\b(?:cat|bat|less|more|head|tail|sed|awk|nl|open|type|gc|get-content|"
                        r"read|read_text|readlines|wc|grep|rg|strings|diff|source|cut|sort|tr)\b")


@dataclass
class RunRequest:
    """一次沙箱运行的固定输入。adapter 只读它，不改它。"""
    prompt: str
    cwd: str
    skill_dir: str | None = None       # 被测 skill（marker / 早停判定用它）
    skill_dirs: tuple = ()             # 附加 skill；空则只用 skill_dir（pi 支持重复 --skill）
    no_skill: bool = False
    model: str | None = None
    thinking: str | None = None
    provider: str | None = None
    early_exit: bool = False
    max_steps: int = 0
    max_tokens: int = 0
    env: dict | None = None             # None = 继承进程环境
    proxy_url: str | None = None

    def skills(self) -> list:
        """要加载的 skill 路径（no_skill / 未指定时为空）。"""
        if self.no_skill:
            return []
        given = [str(s) for s in self.skill_dirs if s]
        if given:
            return given
        return [self.skill_dir] if self.skill_dir else []


@dataclass
class ParseState:
    """一条事件流里的跨行状态；adapter 可在 extra 里放自己的累计量。"""
    skill_marker: str | None = None
    cwd: str | None = None
    early_exit: bool = False
    extra: dict = field(default_factory=dict)


def step_event(tool, args, tool_id=None) -> dict:
    return {"type": STEP, "tool": tool or "?", "args": args, "id": tool_id}


def tool_error_event(tool_id, message: str) -> dict:
    return {"type": TOOL_ERROR, "id": tool_id, "message": str(message)}


def usage_event(tokens) -> dict:
    return {"type": USAGE, "tokens": tokens or 0}


def answer_event(text: str) -> dict:
    return {"type": ANSWER, "text": text}


def skill_loaded_event(path: str | None = None) -> dict:
    return {"type": SKILL_LOADED, "path": path}


def adapter_error_event(message: str) -> dict:
    return {"type": ADAPTER_ERROR, "message": message}


def resolve_executable(program: str) -> str | None:
    """按 PATH 解析可执行文件；Windows 上补试 .cmd/.exe（subprocess 不解析 PATHEXT）。"""
    found = shutil.which(program)
    if found:
        return found
    if sys.platform.startswith("win"):
        for ext in (".cmd", ".exe", ".bat"):
            found = shutil.which(program + ext)
            if found:
                return found
    return None


def probe_version(executable: str, flag: str = "--version", timeout: int = 15) -> str:
    """跑一次 `<exe> --version`，返回最后一行（≤80 字符）；失败返回空串，不抛异常。"""
    try:
        proc = subprocess.run([executable, flag], capture_output=True, text=True,
                              timeout=timeout, errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    lines = [line for line in (proc.stdout or "").strip().splitlines() if line.strip()]
    return lines[-1][:80] if lines else ""


class SkillLoadMatcher:
    """判定一次工具调用是否“加载了被测 skill”。

    停止条件是首次 args 指向被测 SKILL.md 的工具调用（= 加载事件本身），而不是任意首次
    工具调用：后者会在 agent 先探索后加载时把触发 run 误杀成假阴性，系统性低估 recall。

    匹配用 args 的原始值（dict/list 递归取值）而不是 json.dumps——dumps 会把 Windows 路径
    的 \\ 转义成 \\\\，归一化后与 marker 永远对不上（冒烟实测踩坑）。分隔符/连续斜杠/大小写
    归一（模型回读时可能用 / 或 \\）。

    agent 普遍用 `cd <skill目录> && cat SKILL.md` 这种相对路径加载，因此按“路径解析”判：
    相对路径按同一条命令里最近的 `cd` 目标解析，否则按运行 cwd；裸文件名还要求同一条
    简单命令（按管道/`;`/`&&` 切分）里有读取动词——`find . -name SKILL.md | head -20`
    这类只列名字、不读内容的调用不算加载（实测误报两次）。
    """

    def __init__(self, marker: str | None, cwd: str | None = None):
        self.marker = self._absolute(self._norm(marker)) if marker else ""
        self.cwd = self._absolute(self._norm(cwd)) if cwd else ""
        # marker 可传目录（--skill <dir>）或文件（--skill <dir>/SKILL.md）：统一成文件路径
        self.file_marker = ("" if not self.marker else
                            self.marker if self.marker.endswith(".md") else self.marker + "/skill.md")

    @staticmethod
    def _norm(value) -> str:
        return re.sub(r"/+", "/", str(value).replace("\\", "/")).lower()

    @staticmethod
    def _absolute(path: str) -> str:
        """归一化后是真绝对路径才返回（盘符或根目录开头），否则空串。"""
        path = posixpath.normpath(path)
        return path if re.match(r"^[a-z]:/", path) or path.startswith("/") else ""

    @staticmethod
    def _strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from SkillLoadMatcher._strings(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from SkillLoadMatcher._strings(item)

    def _resolve(self, token: str, base: str) -> str:
        got = self._absolute(token)
        if got:
            return got
        return posixpath.normpath(f"{base}/{token}") if base else ""

    def hit(self, args, tool: str | None = None) -> bool:
        if not self.file_marker:
            return False
        if self.marker in self._norm(" ".join(self._strings(args))):
            return True  # 绝对路径直配（read 工具 / 显式传绝对路径）
        for value in self._strings(args):
            base = self.cwd
            # 按管道/命令分隔符切成“简单命令”：读取动词与 SKILL.md 必须落在同一段里
            # （实测误报：`find . -name SKILL.md | head -20` 里的 head 曾让整个调用命中）
            for segment in re.split(r"[;&|\n]+", self._norm(value)):
                tokens = [t for t in re.split(r"[\s'\"`()<>]+", segment) if t]
                for index, token in enumerate(tokens):
                    if token == "cd" and index + 1 < len(tokens):  # 同一串里跟踪 cd 目标
                        base = self._resolve(tokens[index + 1], base) or base
                targets = [t for t in tokens
                           if t.endswith("skill.md") and self._resolve(t, base) == self.file_marker]
                if not targets:
                    continue
                if any("/" in t for t in targets):
                    return True  # 带目录分隔符 = 明确指到文件
                if tool and READ_VERBS.search(str(tool)):
                    return True  # read 工具本身就是读取动词（args 只有裸 SKILL.md）
                if READ_VERBS.search(segment):
                    return True  # bash 裸文件名：同一段里要真有读取动词
        return False


def skill_matcher(state: ParseState) -> SkillLoadMatcher:
    """按 state 缓存 matcher：同一 run 内 marker/cwd 不变，逐行重建纯属浪费。"""
    matcher = state.extra.get("_skill_matcher")
    if matcher is None:
        matcher = SkillLoadMatcher(state.skill_marker, state.cwd)
        state.extra["_skill_matcher"] = matcher
    return matcher


class AgentAdapter:
    """adapter 基类（协议）。

    - `available()` 只探测二进制，不抛异常：返回 (可执行, 版本或不可用原因)。
    - `prepare()` 只操作 request.cwd（如把 skill 装到 agent 认的位置）；无需安装则空实现。
    - `command()` 返回 argv 列表，不拼 shell 字符串。
    - `parse()` 把 agent 的一行输出翻译成规范化事件列表。
    - `finalize()` 返回 adapter 特有 trace 字段；不得覆盖 core 的 trace 字段。
    """
    name = ""
    binary = ""
    capabilities = {"steps": False, "tokens": False, "early_exit": False,
                    "skill_flag": False, "model_flag": False}

    def available(self) -> tuple:
        raise NotImplementedError

    def prepare(self, request: RunRequest) -> None:
        return None

    def command(self, request: RunRequest) -> list:
        raise NotImplementedError

    def parse(self, line: str, state: ParseState) -> list:
        raise NotImplementedError

    def finalize(self, state: ParseState) -> dict:
        return {}
