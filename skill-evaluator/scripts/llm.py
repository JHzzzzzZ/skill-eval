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
import urllib.request

import _console

_console.fix()

import _config


class LLMError(RuntimeError):
    """配置缺失、网络错误、协议/响应不合法。"""


def protocol() -> str:
    value = _config.get_str("llm.protocol", "openai").strip().lower()
    if value not in ("openai", "anthropic"):
        raise LLMError(f"llm.protocol 只支持 openai/anthropic，实际是 {value!r}")
    return value


def base_url(override: str | None = None) -> str:
    value = (override if override is not None else _config.get_str("llm.base_url", "")).strip()
    if not value:
        raise LLMError("llm.base_url 未配置：请在 config.yaml 填端点，或给 --base-url")
    return value.rstrip("/")


def default_model(override: str | None = None) -> str:
    value = (override if override is not None else _config.get_str("llm.model", "")).strip()
    if not value:
        value = os.environ.get("SKILL_EVAL_MODEL", "").strip()
    if not value:
        raise LLMError("未指定模型：请给 --model，或在 config.yaml 填 llm.model / 设 SKILL_EVAL_MODEL")
    return value


def api_key(override: str | None = None) -> str:
    if override:
        return override
    env_name = _config.get_str("llm.api_key_env", "").strip()
    return os.environ.get(env_name, "") if env_name else ""


def configured() -> bool:
    """T1 是否具备直连条件（端点 + 模型）；缺一不可。"""
    try:
        base_url()
        default_model()
        return True
    except LLMError:
        return False


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
         timeout: int | None = None, opener=None) -> dict:
    """单次无工具对话，返回 {text, model, protocol, request, response}。"""
    proto = protocol()
    root = base_url(base_url_override)
    model_id = default_model(model)
    key = api_key(api_key_override)
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
