# 执行适配器 / 沙箱 / 限流实现方案

状态：**已执行并验收（2026-11-02）；模型代理除外**。对应设计：
`execution-adapter.md`、`sandbox-and-rate-limit.md`、`access-and-audit.md`。

## 目标

- 换 agent（Claude Code 等）只加 adapter 文件，不改 runner/report/score 主逻辑。
- T2/T3 在容器内运行；无容器运行时 fail closed，T0/T1 不受影响。
- 游客 T1 与 token T2/T3 有持久化额度；超限 429，不创建 run。

## 阶段 A：Adapter 抽象

改动范围：

- 新增 `scripts/adapter_contract.py`：`RunRequest`、`AgentAdapter` 协议、规范化事件。
- 新增 `scripts/adapter_pi.py`：把 `build_args`、pi 事件解析、skill 加载判定搬进来。
- 新增 `scripts/adapter_claude.py`：`claude -p --output-format stream-json` 命令 + 事件映射；`available()` 只探测二进制。
- 新增 `scripts/adapter_registry.py`：注册表、`get/available`。
- 改 `scripts/trace_run.py`：通用聚合循环 + `--adapter`；`EventAggregator` 默认 pi 兼容旧测试；`--events --adapter`。
- 改 `scripts/harness.py`：不再硬编码 pi；`--harness` 作为 `--adapter` 别名传给 trace_run。
- 改 `tests/test_trace_run.py`：保留 pi 断言；新增 fake Claude 事件流、注册表、缺 step/token 降级。
- 改 `scripts/AGENTS.md`、`tests/AGENTS.md` 计数。

验收：

- 现有 pi 行为与 trace 字段不变。
- `--build-only --adapter claude-code` 输出 claude 命令。
- fake Claude JSONL 能产出 steps/tokens/answer/early_exit。
- 未知 adapter 明确报错；不静默回退 pi。

## 阶段 B：限流

改动范围：

- 新增 `web/ratelimit.py`：SQLite 固定窗口；默认额度；token 覆盖；`check/record/cleanup`。
- 新增 `web/limits.json`（默认值）+ `web/limits.example.json`。
- 改 `web/app.py`：`/api/run` 在创建目录前检查；429 + `Retry-After`；审计 `rate_limited`；ACTIVE 记录 owner 以算并发。
- 改 `web/index.html`：429 错误提示“配额用完/稍后重试”，不吞错误。
- 改 `tests/test_web.py`：游客 T1 超限、token 覆盖、并发、429 不建 run、审计。
- 改 `.gitignore`：`web/usage.sqlite3*`；`web/limits.json` 若作为本地配置则 ignore，保留 example。
- 改 `skill-evaluator/reference.md`、`AGENTS.md`。

默认额度：

| actor | tier | 小时 | 天 | 并发 |
|---|---|---|---|---|
| guest | t0 | 20 | 60 | 1 |
| guest | t1 | 3 | 10 | 1 |
| token | t0/t1 | 20 | 50 | 1 |
| token | t2a/t2b/t3 | 3 | 8 | 2 |
| global | all | - | - | 4 |

验收：

- 第 N+1 次 429，`Retry-After > 0`，run 目录不创建。
- 游客 cookie 变更不能绕过 IP 限制；token 额度独立。
- 重启服务后计数仍生效。
- 限流失败不阻断 T0/T1 已有报告读取。

## 阶段 C：Sandbox + 依赖

改动范围：

- 新增 `scripts/sandbox.py`：`DockerBackend` 命令构造、可用性探测、路径映射（host workdir → `/work`）。
- 新增 `scripts/capabilities.py`：`agent + sandbox + tier` 能力矩阵，供 CLI/Web 共用。
- 改 `scripts/trace_run.py`：`--sandbox docker` 包裹 agent 命令；无 Docker 时明确错误；guard 仅 host 模式。
- 改 `scripts/runner.py`：T2/T3 启动前 `capabilities.require(tier, adapter, sandbox)`；失败退出码 3，不写阶段缓存。
- 改 `scripts/check-deps.sh`：`--tier` / `--json`；无参数只查基础依赖；T2/T3 加查 Docker daemon、镜像、代理。
- 改 `web/app.py`：`GET /api/capabilities`；`POST /api/run` T2/T3 缺依赖 412（`dep_missing`），不建目录、不扣配额；UI 显示原因。
- 改 `web/index.html`：能力提示；T2/T3 不可用时禁用/阻止启动。
- 新增 `sandbox/Dockerfile`（固定基础镜像 + pi/claude 可选）、`sandbox/README.md`。
- 新增 `tests/test_sandbox.py`、`tests/test_capabilities.py`；改 `test_web.py` 用 monkeypatch 提供能力。
- 改 `README*` / `AGENTS.md` / `reference.md`。

验收：

- 当前机器无 Docker：`check-deps.sh --tier t2a` 退出 1；T2/T3 HTTP 412；T0/T1 200。
- fake Docker 命令包含 `--network none`、`--read-only`、`--cap-drop ALL`、内存/CPU/pids 限制、无宿主凭据挂载。
- 容器路径 `/work`，不泄露宿主绝对路径。
- 有 Docker 时 T2/T3 可跑；无 Docker 时绝不回退宿主。

## 执行顺序

1. A（adapter）先落地，保持测试绿。
2. B（rate limit）与 A 文件不重叠，可并行；只改 web + tests。
3. A/B 完成后做 C（sandbox/deps），它依赖 A 的 `--adapter` 与 B 的 `/api/run`。
4. 父代理统一验收、跑全量、修集成问题、提交。

## 风险

- Claude Code CLI 参数/事件格式可能与本机版本不同；adapter 先用 fake 事件测试，真机需一次冒烟。
- 当前没有 Docker，容器只能做命令构造与 fake 测试；真实隔离冒烟延后。
- 新增 `scripts/*.py` 会改变评估器指纹，历史报告 `comparable=false` 属预期。
