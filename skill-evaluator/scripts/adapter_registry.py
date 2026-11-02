"""执行适配器注册表：唯一发现入口（阶段 A）。

未知 adapter 明确报错，不静默回退 pi；trace/report/score 不感知具体 agent。
新增 agent = 新增 `adapter_<name>.py` + 这里一行注册（设计见 docs/design/execution-adapter.md）。
"""
from __future__ import annotations

import _console

_console.fix()

from adapter_claude import ClaudeCodeAdapter
from adapter_pi import PiAdapter

DEFAULT = "pi"

_ADAPTERS = {adapter.name: adapter for adapter in (PiAdapter(), ClaudeCodeAdapter())}


class UnknownAdapterError(ValueError):
    """未注册的 adapter；调用方应显式失败，而不是退回默认载体。"""


def names() -> list:
    return sorted(_ADAPTERS)


def get(name: str | None = None):
    key = (name or DEFAULT).strip()
    if key not in _ADAPTERS:
        raise UnknownAdapterError(f"未知执行适配器 {key!r}；可用：{', '.join(names())}")
    return _ADAPTERS[key]


def is_available(name: str) -> bool:
    """已注册且二进制可探测；未知 adapter 返回 False（探测失败不等于回退）。"""
    if name not in _ADAPTERS:
        return False
    return bool(_ADAPTERS[name].available()[0])


def available() -> list:
    """已注册且二进制可用的 adapter 名（UI/依赖检查用；探测失败不抛）。"""
    return [name for name in names() if is_available(name)]
