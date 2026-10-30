"""LLM 接口层（ADR-0034）：协议、无 tools、JSON 提取。"""
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import llm


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_extract_json_nested_and_surrounding_text():
    text = '前置说明 {"items":[{"name":"a","pass":true,"quote":"x"}],"score":1} 尾巴'
    obj = llm.extract_json(text)
    assert obj["items"][0]["name"] == "a" and obj["score"] == 1


def test_extract_json_returns_none_without_object():
    assert llm.extract_json("没有 JSON") is None


def test_openai_request_has_no_tools_and_extracts_text():
    seen = {}

    def opener(req, timeout):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp({"choices": [{"message": {"content": " 答案 "}}]})

    out = llm.chat("问题", model="m", base_url_override="http://local/v1",
                   opener=opener)
    assert out["text"] == "答案"
    assert seen["url"] == "http://local/v1/chat/completions"
    assert "tools" not in seen["body"] and seen["body"]["temperature"] == 0
    assert seen["body"]["messages"][-1] == {"role": "user", "content": "问题"}


def test_anthropic_request_and_response(monkeypatch):
    monkeypatch.setattr(llm, "protocol", lambda override=None: "anthropic")
    seen = {}

    def opener(req, timeout):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp({"content": [{"type": "text", "text": "anthropic 答案"}]})

    out = llm.chat("问题", model="m", base_url_override="http://local/v1",
                   opener=opener)
    assert out["text"] == "anthropic 答案"
    assert seen["url"] == "http://local/v1/messages"
    assert seen["body"]["messages"][-1]["content"] == "问题"


def test_default_base_url_is_official():
    assert llm.base_url("", "openai") == "https://api.openai.com/v1"
    assert llm.base_url("", "anthropic") == "https://api.anthropic.com/v1"
