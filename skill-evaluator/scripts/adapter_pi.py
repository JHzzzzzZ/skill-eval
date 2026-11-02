"""pi 执行适配器：命令构造、`pi --mode json` 事件翻译、skill 加载判定。

从 trace_run.py 搬来的既有行为（命令形状、事件字段、早停口径）保持不变；
core 只消费 adapter_contract 的规范化事件。
"""
from __future__ import annotations

import _console

_console.fix()

import json
import os
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

DEFAULT_MODEL_ENV = "SKILL_EVAL_MODEL"  # 环境变量作默认模型，--model 显式覆盖


class PiAdapter(AgentAdapter):
    name = "pi"
    binary = "pi"
    capabilities = {"steps": True, "tokens": True, "early_exit": True,
                    "skill_flag": True, "model_flag": True}

    def available(self) -> tuple:
        executable = resolve_executable(self.binary)
        if not executable:
            return False, "pi CLI 不存在：请先安装 pi（npm i -g @earendil-works/pi-coding-agent）"
        return True, probe_version(executable) or Path(executable).name

    def command(self, request: RunRequest) -> list:
        return build_args(request)

    def parse(self, line: str, state: ParseState) -> list:
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            return [adapter_error_event(f"非法事件行: {exc}")]
        if not isinstance(event, dict) or "type" not in event:
            return [adapter_error_event(f"事件缺 type 字段: {line[:80]}")]
        kind = event["type"]
        if kind == "tool_execution_start":
            raw_args = event.get("args")
            events = [step_event(event.get("toolName", "?"), raw_args, event.get("toolCallId"))]
            hit = state.early_exit and skill_matcher(state).hit(raw_args, event.get("toolName"))
            if hit:
                events.append(skill_loaded_event(state.skill_marker))
            return events
        if kind == "tool_execution_end" and event.get("isError"):
            return [tool_error_event(event.get("toolCallId"), str(event.get("result"))[:200])]
        if kind == "turn_end":
            usage = event.get("usage") or {}
            return [usage_event(usage.get("totalTokens") or usage.get("tokens", {}).get("total") or 0)]
        if kind == "agent_end":
            events = []
            for message in event.get("messages", []):
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    continue
                for content in message.get("content", []):
                    if isinstance(content, dict) and content.get("type") == "text" and content.get("text"):
                        events.append(answer_event(content["text"]))  # ISS-3: 最终回答全文
                events.append(usage_event((message.get("usage") or {}).get("totalTokens") or 0))
            return events
        return []


def build_args(request: RunRequest) -> list:
    """pi CLI 命令。模型：--model 显式传参 > SKILL_EVAL_MODEL 环境变量 > pi 默认。

    skill 可重复传 --skill（补丁 B5）。始终带 --no-skills（关闭 skill 自动发现）：否则
    发现到的全局同名 skill 会屏蔽被测版本（故 --skill 会被静默忽略），测量不再可复现（补丁 B3）。
    """
    cmd = ["pi", "--mode", "json", "--no-session", "--no-skills"]
    for skill in request.skills():
        # 解析为绝对路径：子进程 cwd=worktree，相对路径会按 worktree 解析
        # 导致 skill 静默不加载（实测冒烟踩坑）
        cmd += ["--skill", str(Path(skill).resolve())]
    effective_model = request.model or os.environ.get(DEFAULT_MODEL_ENV)
    if effective_model:
        if request.provider and "/" not in effective_model:
            cmd += ["--provider", request.provider]
        cmd += ["--model", effective_model]
    if request.thinking:
        cmd += ["--thinking", request.thinking]
    cmd += ["--", request.prompt]
    return cmd
