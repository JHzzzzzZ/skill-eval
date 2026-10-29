"""config.yaml 读取器（零依赖 YAML 子集，ADR-0035）。

只支持 config.yaml 里演示的子集：两级键、标量、行内 # 注释、行内 [a, b] 列表。
不支持锚点、多行块、嵌套列表。格式错误抛 ConfigError（带行号），不静默降级。

为什么不用 PyYAML：运行时零第三方依赖是硬约束（ADR-0035）。
为什么不用 JSON/TOML：用户要 YAML，且注释是这次的主要诉求。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

_DATA = None
_SOURCE = None


class ConfigError(ValueError):
    """config.yaml 缺失以外的格式/类型错误；启动时直接抛，避免用错阈值。"""


def _strip_comment(line: str) -> str:
    """去掉行内 # 注释；引号内的 # 不算注释。"""
    quote = None
    escaped = False
    for i, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#":
            return line[:i].rstrip()
    return line.rstrip()


def _split_list(text: str) -> list:
    """按逗号拆行内列表；引号内的逗号不拆。"""
    out, cur, quote = [], [], None
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            cur.append(ch)
        elif ch == ",":
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur).strip())
    return [x for x in out if x != ""]


def _scalar(text: str, lineno: int):
    """解析标量：引号字符串、bool、null、int、float、行内列表、裸字符串。"""
    s = text.strip()
    if s == "":
        return None
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [] if not inner else [_scalar(x, lineno) for x in _split_list(inner)]
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?(?:\d+\.\d*|\.\d+)", s):
        return float(s)
    return s


def parse(text: str, source: str = "config.yaml") -> dict:
    """解析受控子集；返回嵌套 dict。"""
    data = {}
    section = None
    seen = set()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        if "\t" in line[:indent + 1]:
            raise ConfigError(f"{source}:{lineno}: 用空格缩进，不要用 tab")
        body = line.strip()
        if indent == 0:
            key, sep, value = body.partition(":")
            key = key.strip()
            if not sep or not key:
                raise ConfigError(f"{source}:{lineno}: 顶层必须是 key: 或 key: value")
            if key in seen:
                raise ConfigError(f"{source}:{lineno}: 重复键 {key}")
            seen.add(key)
            if value.strip():
                data[key] = _scalar(value, lineno)
                section = None
            else:
                data[key] = {}
                section = key
        elif indent == 2:
            if section is None:
                raise ConfigError(f"{source}:{lineno}: 缩进键必须属于一个顶层分节")
            key, sep, value = body.partition(":")
            key = key.strip()
            if not sep or not key:
                raise ConfigError(f"{source}:{lineno}: 分节内必须是 key: value")
            if key in data[section]:
                raise ConfigError(f"{source}:{lineno}: 重复键 {section}.{key}")
            data[section][key] = _scalar(value, lineno)
        else:
            raise ConfigError(f"{source}:{lineno}: 只支持两级键（缩进 2 空格）")
    return data


def load(path=None, *, force: bool = False) -> dict:
    """读 config.yaml；缺文件返回空 dict（走代码默认值），格式错抛 ConfigError。"""
    global _DATA, _SOURCE
    if _DATA is not None and not force and path is None:
        return _DATA
    p = Path(path) if path is not None else DEFAULT_PATH
    _SOURCE = p
    if not p.is_file():
        _DATA = {}
        return _DATA
    try:
        text = p.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"读不到 {p}: {exc}") from exc
    _DATA = parse(text, str(p))
    return _DATA


def reload(path=None) -> dict:
    """测试/长驻进程用：清缓存后重读。"""
    global _DATA
    _DATA = None
    return load(path, force=True)


def get(key: str, default=None):
    """按点号取两级键，如 static.description_warn_chars。"""
    cur = load()
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _typed(key: str, default, types, type_name: str):
    value = get(key, default)
    if isinstance(value, bool) and bool not in types:
        raise ConfigError(f"{key} 必须是{type_name}，实际是布尔值")
    if not isinstance(value, types):
        raise ConfigError(f"{key} 必须是{type_name}，实际是 {value!r}")
    return value


def get_int(key: str, default: int) -> int:
    return _typed(key, default, (int,), "整数")


def get_float(key: str, default: float) -> float:
    return _typed(key, default, (int, float), "数字")


def get_bool(key: str, default: bool) -> bool:
    return _typed(key, default, (bool,), "布尔值")


def get_str(key: str, default: str) -> str:
    return _typed(key, default, (str,), "字符串")


def get_list(key: str, default: list) -> list:
    return _typed(key, default, (list,), "列表")


def pick(cli_value, env_name: str | None, key: str, default):
    """优先级：CLI > 现有环境变量 > config.yaml > 默认值（ADR-0035）。"""
    if cli_value is not None:
        return cli_value
    if env_name:
        env_value = os.environ.get(env_name)
        if env_value not in (None, ""):
            return env_value
    return get(key, default)
