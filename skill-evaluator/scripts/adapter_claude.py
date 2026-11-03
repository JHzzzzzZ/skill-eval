"""Claude Code 执行适配器（阶段 A；真机未验证，事件格式按 stream-json 公开契约实现）。

命令：`claude -p <prompt> --output-format stream-json --verbose`。
事件：assistant 的 tool_use → step；user 的 tool_result(is_error) → tool_error；
result → 最终 answer 与本次运行 usage。token 缺失时保持 0（不伪造），errors 不因此失败。
中间 assistant 文本不进 answer：trace.answer 是给 compare/T3 比对的**最终**回答，只有 result 权威。

`available()` 只探测二进制，不要求本机安装；本机没装时返回 (False, 安装提示)。
"""
from __future__ import annotations

import _console

_console.fix()

import json
import shutil
from pathlib import Path

from adapter_contract import (
    AgentAdapter,
    ParseState,
    RunRequest,
    adapter_error_event,
    answer_event,
    probe_version,
    resolve_executable,
    skill_loaded_event,
    skill_matcher,
    step_event,
    tool_error_event,
    usage_event,
)

# Claude Code 的项目级 skill 目录（相对运行 cwd）
CLAUDE_SKILL_DIR = ".claude"  # static-check:ignore


class ClaudeCodeAdapter(AgentAdapter):
    name = "claude-code"
    binary = "claude"
    capabilities = {"steps": True, "tokens": True, "early_exit": True,
                    "skill_flag": False, "model_flag": True}

    def available(self) -> tuple:
        executable = resolve_executable(self.binary)
        if not executable:
            return False, "claude CLI 不存在：请先安装 Claude Code（npm i -g @anthropic-ai/claude-code）"
        return True, probe_version(executable) or Path(executable).name

    def prepare(self, request: RunRequest) -> None:
        """把被测 skill 装到运行 cwd 的项目级 skill 目录；只操作 request.cwd。"""
        if request.no_skill or not request.skill_dir or not request.cwd:
            return
        source = Path(request.skill_dir)
        source_dir = source.parent if source.is_file() else source
        if not source_dir.is_dir():
            return
        destination = Path(request.cwd) / CLAUDE_SKILL_DIR / "skills" / source_dir.name  # static-check:ignore
        if destination.resolve() == source_dir.resolve():
            return
        shutil.copytree(source_dir, destination, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(CLAUDE_SKILL_DIR, ".git", "results", "__pycache__", "_internal"))

    def command(self, request: RunRequest) -> list:
        cmd = [self.binary, "-p", request.prompt, "--output-format", "stream-json", "--verbose"]
        if request.model:
            cmd += ["--model", request.model]
        return cmd

    def parse(self, line: str, state: ParseState) -> list:
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            return [adapter_error_event(f"非法事件行: {exc}")]
        if not isinstance(event, dict) or "type" not in event:
            return [adapter_error_event(f"事件缺 type 字段: {line[:80]}")]
        kind = event["type"]
        if kind == "assistant":
            return self._assistant(event.get("message") or {}, state)
        if kind == "user":
            return self._tool_results(event.get("message") or {})
        if kind == "result":
            return self._result(event, state)
        return []  # system/init 等：不影响 trace

    def finalize(self, state: ParseState) -> dict:
        cost = state.extra.get("cost_usd")
        return {"cost_usd": cost} if isinstance(cost, (int, float)) else {}

    @staticmethod
    def _assistant(message: dict, state: ParseState) -> list:
        events = []
        for content in message.get("content") or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") != "tool_use":
                continue  # 中间文本不进 answer（result 的 result 字段才是最终回答）
            events.append(step_event(content.get("name", "?"), content.get("input"),
                                     content.get("id")))
            if state.early_exit and skill_matcher(state).hit(content.get("input"),
                                                             content.get("name")):
                events.append(skill_loaded_event(state.skill_marker))
        tokens = state.extra.get("tokens", 0) + _usage_tokens(message.get("usage"))
        if tokens:
            state.extra["tokens"] = tokens
            events.append(usage_event(tokens))
        return events

    @staticmethod
    def _tool_results(message: dict) -> list:
        events = []
        for content in message.get("content") or []:
            if not isinstance(content, dict) or content.get("type") != "tool_result":
                continue
            if not content.get("is_error"):
                continue
            payload = content.get("content")
            text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            events.append(tool_error_event(content.get("tool_use_id"), text[:200]))
        return events

    @staticmethod
    def _result(event: dict, state: ParseState) -> list:
        if event.get("is_error"):
            detail = event.get("result") or event.get("subtype") or "未知错误"
            return [adapter_error_event(f"claude 运行失败: {str(detail)[:300]}")]
        events = []
        text = event.get("result")
        if isinstance(text, str) and text.strip():
            events.append(answer_event(text))
        tokens = _usage_tokens(event.get("usage"))
        if tokens:
            state.extra["tokens"] = tokens
            events.append(usage_event(tokens))
        cost = event.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            state.extra["cost_usd"] = cost
        return events


def _usage_tokens(usage) -> int:
    """input+output；cache 命中量不计入（与 pi 的 totalTokens 口径可比）。缺字段=0，不伪造。"""
    if not isinstance(usage, dict):
        return 0
    total = 0
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            total += value
    return total
