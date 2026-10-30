"""LLM 接口层（ADR-0034）：T1 直连模型，不经过 agent 框架。

支持两种协议（config.yaml → llm.protocol）：
- openai：POST <base_url>/chat/completions（OpenAI-compatible）
- anthropic：POST <base_url>/messages

不发送 tools/tool_choice，结构上保证评审“无工具”（ADR-0022）。
凭据只从 llm.api_key_env 指定的**已存在**环境变量读取，或由调用方一次性传入；
本模块不写任何凭据到磁盘/报告。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request  # static-check:ignore

import _console

_console.fix()

import _config


class LLMError(RuntimeError):
    """配置缺失、网络错误、协议/响应不合法。"""


def protocol(override: str | None = None) -> str:
    value = (override or _config.get_str("llm.protocol", "openai")).strip().lower()
    if value not in ("openai", "anthropic"):
        raise LLMError(f"llm.protocol 只支持 openai/anthropic，实际是 {value!r}")
    return value


def _env_base_url(proto: str) -> str:
    return os.environ.get("ANTHROPIC_BASE_URL" if proto == "anthropic" else "OPENAI_BASE_URL", "").strip()


def base_url(override: str | None = None, proto: str | None = None) -> str:
    """UI/CLI > config.yaml > 标准环境变量 > 官方端点。"""
    proto = proto or protocol()
    value = (override or "").strip()
    if not value:
        value = _config.get_str("llm.base_url", "").strip()
    if not value:
        value = _env_base_url(proto)
    if not value:
        value = "https://api.anthropic.com/v1" if proto == "anthropic" else "https://api.openai.com/v1"
    return value.rstrip("/")


def default_model(override: str | None = None) -> str:
    value = (override if override is not None else _config.get_str("llm.model", "")).strip()
    if not value:
        value = os.environ.get("SKILL_EVAL_MODEL", "").strip()
    if not value:
        raise LLMError("未指定模型：请给 --model，或在 config.yaml 填 llm.model / 设 SKILL_EVAL_MODEL")
    return value


def api_key(override: str | None = None, proto: str | None = None) -> str:
    if override:
        return override
    env_name = _config.get_str("llm.api_key_env", "").strip()
    if env_name:
        value = os.environ.get(env_name, "")
        if value:
            return value
    proto = proto or protocol()
    return os.environ.get("ANTHROPIC_API_KEY" if proto == "anthropic" else "OPENAI_API_KEY", "")


def configured() -> bool:
    """T1 是否具备直连条件：有模型；且端点显式配置或存在标准 API key。"""
    try:
        default_model()
    except LLMError:
        return False
    proto = protocol()
    if _config.get_str("llm.base_url", "").strip() or _env_base_url(proto):
        return True
    return bool(api_key(proto=proto))


def _endpoint(proto: str, root: str) -> str:
    if proto == "anthropic":
        return root if root.endswith("/messages") else root + "/messages"
    return root if root.endswith("/chat/completions") else root + "/chat/completions"


def _request_body(proto: str, model: str, prompt: str, system: str | None) -> dict:
    messages = [{"role": "user", "content": prompt}]
    if proto == "anthropic":
        body = {"model": model, "max_tokens": 4096, "temperature": 0,
                "messages": messages}
        if system:
            body["system"] = system
        return body
    body = {"model": model, "messages": ([{"role": "system", "content": system}] if system else []) + messages,
            "temperature": 0, "stream": False}
    return body


def _headers(proto: str, key: str) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if key:
        if proto == "anthropic":
            headers["x-api-key"] = key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {key}"
    return headers


def _extract_text(proto: str, payload) -> str:
    if not isinstance(payload, dict):
        raise LLMError("LLM 响应不是 JSON 对象")
    if proto == "anthropic":
        blocks = payload.get("content")
        if isinstance(blocks, list):
            texts = [b.get("text", "") for b in blocks
                     if isinstance(b, dict) and b.get("type") == "text"]
            return "".join(texts).strip()
        raise LLMError("anthropic 响应缺 content 数组")
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return "".join(c.get("text", "") for c in content
                           if isinstance(c, dict) and c.get("type") == "text").strip()
    raise LLMError("openai 响应缺 choices[0].message.content")


def chat(prompt: str, *, model: str | None = None, system: str | None = None,
         api_key_override: str | None = None, base_url_override: str | None = None,
         timeout: int | None = None, opener=None, protocol_override: str | None = None) -> dict:
    """单次无工具对话，返回 {text, model, protocol, request, response}。"""
    proto = protocol(protocol_override)
    root = base_url(base_url_override, proto)
    model_id = default_model(model)
    key = api_key(api_key_override, proto)
    body = _request_body(proto, model_id, prompt, system)
    url = _endpoint(proto, root)
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),  # static-check:ignore
                                 headers=_headers(proto, key), method="POST")
    timeout = timeout or _config.get_int("run.timeout_s", 300)
    open_fn = opener or urllib.request.urlopen  # static-check:ignore
    try:
        with open_fn(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise LLMError(f"LLM HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"LLM 网络错误: {exc.reason}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError(f"LLM 响应不是 JSON: {raw[:300]}") from exc
    text = _extract_text(proto, payload)
    if not text:
        raise LLMError("LLM 返回空文本")
    return {"text": text, "model": model_id, "protocol": proto,
            "request": body, "response": payload}


def ask(prompt: str, **kwargs) -> str:
    return chat(prompt, **kwargs)["text"]


def status() -> dict:
    """前端初始化用：只返回非敏感配置。"""
    try:
        proto = protocol()
        return {"protocol": proto, "base_url": base_url(proto=proto),
                "model": _config.get_str("llm.model", "") or os.environ.get("SKILL_EVAL_MODEL", ""),
                "api_key_env": _config.get_str("llm.api_key_env", ""),
                "configured": configured()}
    except LLMError as exc:
        return {"protocol": "openai", "base_url": "", "model": "",
                "api_key_env": "", "configured": False, "error": str(exc)}


def extract_json(text: str):
    """从模型文本里提取第一个花括号配对完整的 JSON 对象（支持嵌套与字符串内花括号）。"""
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
                        break
        start = text.find("{", start + 1)
    return None
