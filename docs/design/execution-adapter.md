# 执行适配器抽象（Agent Adapter + Sandbox Backend）

状态：**已实现并验收（2026-11-02）**。pi 适配器保留原行为；Claude Code 已完成真机冒烟（`claude -p` 事件流、early-exit、tokens）。目标是让 pi 换成 Claude Code / Codex / 其他 agent 时，只新增一个 adapter 文件，不改 runner/report/score/前端。

## 1. 现状的问题

`harness.py` 名字像抽象，实际只有 pi：`IMPLEMENTED = ("pi",)`，`_cmd()` 固定拼 `trace_run.py`，`trace_run.py` 里 `EventAggregator` 直接解析 pi 的 `tool_execution_start` / `turn_end` / `agent_end`。

要换 Claude Code，今天必须改 `harness.py`、`trace_run.py`、事件解析、参数拼接、早停判定，甚至 report 数据源。这不是抽象。

## 2. 两个正交维度

不要把“agent”和“沙箱”混成一个 `harness`：

- **AgentAdapter**：pi / claude-code / codex，负责命令、事件、模型参数、skill 安装、早停能力。
- **SandboxBackend**：host-unsafe / docker / vm，负责把 agent 命令放进隔离环境。

组合方式：

```text
trace_run --adapter claude-code --sandbox docker
```

T2/T3 只允许 `sandbox=docker`（或 vm）；`host-unsafe` 只能显式开发用。

## 3. AgentAdapter 契约

每个 adapter 一个文件，实现：

```python
class Adapter:
    name = "claude-code"
    capabilities = {
        "steps": True,        # 能给出工具调用
        "tokens": True,       # 能给出 token 用量
        "early_exit": True,   # 能检测 skill 加载
        "skill_flag": False,  # 是否原生 --skill
        "model_flag": True,
    }

    def available(self) -> tuple[bool, str]:
        """返回 (可执行, 版本/原因)。不要抛异常。"""

    def prepare(self, request) -> None:
        """把 skill 放到 agent 认的位置（如 .claude/skills/），
        或声明无需安装。只操作 request.workdir。"""

    def command(self, request) -> list[str]:
        """返回 argv。不要在内部拼 shell 字符串。"""

    def parse(self, line: str, state) -> list[dict]:
        """把 agent 的一行输出翻译成规范化事件。"""

    def finalize(self, state) -> dict:
        """补充 answer/tokens/raw 等 adapter 特有字段。"""
```

`RunRequest` 固定字段：`prompt/cwd/skill_dir/no_skill/model/early_exit/max_steps/max_tokens/env/proxy_url`。

规范化事件（core 只认这几种）：

```json
{"type":"step","tool":"read","args":{},"id":"..."}
{"type":"tool_error","id":"...","message":"..."}
{"type":"usage","tokens":123}
{"type":"answer","text":"..."}
{"type":"skill_loaded","path":"..."}
{"type":"adapter_error","message":"..."}
```

core 负责：步数/token 上限、超时、杀进程树、early-exit 停止、trace schema。adapter 负责：怎么启动、怎么把事件翻译过来、怎么判“加载了 skill”。

## 4. trace 契约不变

`trace_run` 输出仍保持现有字段：

```json
{"steps": [], "tool_calls": 0, "tokens": 0, "seconds": 0.0,
 "answer": "", "errors": [], "triggered": false, "passed": true,
 "early_exit": false, "limit_hit": null,
 "adapter": "pi", "adapter_version": "..."}
```

- `score.py` / `report.py` 只看这些字段，不感知 pi/Claude Code。
- adapter 给不出 token/step 时，字段缺省，对应指标 `skipped`，不伪造 0。
- `raw` 可选，存 adapter 原始事件（受大小上限），不进入报告正文。

## 5. 接入一个新 agent 的最小工作量

以 Claude Code 为例：

1. 新增 `scripts/adapters/claude_code.py`，实现 `available/command/parse/finalize`。
2. 在 `adapters/registry.py` 注册一行。
3. 加一个 fake CLI 事件流测试（不依赖真 Claude Code）。
4. 不改 `runner.py`、`report.py`、`score.py`、`web/` 主逻辑。

如果 Claude Code 只能给纯文本、没有工具事件：

- `capabilities.steps=False`、`early_exit=False`；
- T2A 不早停，走完整 run + `trigger_judge`；
- T3 的 #13/#15 因缺数据标 `skipped`，不伪造；
- 仍然可跑 T2B/T3 的答案对比。

## 6. 兼容与迁移

- 保留 `--harness pi` 作为 `--adapter pi` 的别名；旧 UI/脚本不立刻断。
- 现有 `EventAggregator` 的 pi 解析移到 `adapters/pi.py`；`trace_run` 只保留通用循环。
- `harness.available()` 改为 adapter 注册表 + 二进制探测；UI 动态列出可用 adapter。
- 依赖检查按 `adapter + sandbox + tier` 组合：T2/T3 缺 adapter 或 Docker 都 412 fail closed；T0/T1 不受影响。
- 容器内安装哪个 agent 由镜像决定；adapter 只负责命令，不负责镜像构建。

## 7. 验收

- pi adapter 重构后现有 T2/T3 测试全绿，trace 字段不变。
- 用一个 fake adapter（模拟 Claude Code 事件）跑通 T2A/T2B/T3：不改核心文件，只注册 adapter。
- 无 step/token 的 adapter：`#13/#15` 为 skipped，报告不崩。
- UI 选择 adapter 后 T2/T3 依赖检查正确；不可用 adapter 不显示或标灰。
- 新增 agent 的 diff 只包含 `adapters/<name>.py` + 注册行 + 测试。
