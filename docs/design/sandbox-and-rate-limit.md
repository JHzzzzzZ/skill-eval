# T2/T3 沙箱与限流方案

状态：**设计，待实现**。T0/T1 游客可用；T2/T3 需要 token。当前 `trace_run` 只是 cwd 副本，不是安全边界，不能把 token 当沙箱。

## 1. T2/T3 沙箱

### 推荐：Docker / Podman + WSL2 Linux 容器

Windows 上用 Docker Desktop（WSL2 backend）或 Podman；T2/T3 每个 case 一次一次性容器。不要用 Windows Job Object / 低权限用户冒充强隔离。

容器规格：

- 镜像固定 digest，内含 Python ≥3.9、git、pi CLI、最小 shell；构建时锁版本。
- 非 root 用户；rootfs `--read-only`；`/tmp` tmpfs；`/work` 只挂当前 skill 副本（可写）；`/out` 单独可写目录用于回收产物。
- 不挂宿主 `$HOME`、`~/.pi`、SSH/云凭据、Docker socket、源码仓库；不启用 host PID/network。
- `--cap-drop ALL --security-opt no-new-privileges`；默认 seccomp；`--pids-limit`、`--memory`、`--cpus`、`--ulimit`。
- 默认 `--network none`；模型访问走宿主侧代理（见下），不给容器真实 API key。
- 每个 case/arm 新容器；结束 `docker rm -f`，不跨 case 复用文件系统。

### 模型访问

容器不能拿真实 key。宿主起 `scripts/model_proxy.py`（仅监听回环/专用 bridge），容器只拿到一个短期代理 token：

- 代理只允许 `/v1/chat/completions` 等模型端点，注入真实 key；记录 model/token/延迟，不记录 prompt 正文。
- 容器网络只可达代理，不能访问公网；需要联网的被测 skill 默认判失败/受限，后续再做域名 allowlist。
- 代理按 token/run 限流，超限返回 429。

### 与 runner 集成

- 新增 harness adapter `container`；`harness.run_case` 生成 `docker run ... pi --mode json ...`，stdout 仍交给 `trace_run.EventAggregator`。
- T2A：每个触发 case 一个容器；early-exit 杀容器进程树。
- T2B：每个 case 的 with/baseline 两个独立容器；同 cwd/同输入；不共享状态。
- T3：每个 case 一个容器，重复运行在同一容器内完成（幂等要测“同环境重复”，不能每次重置）；case 结束销毁。
- 产物只从 `/out` 回收；trace 由 stdout 采集；不把宿主 results 目录挂进容器。
- 资源上限沿用 `run.trigger_max_steps/tokens/timeout`，再加容器 CPU/内存/时间；超限杀容器并记 `skipped`。
- 没有容器运行时：T2/T3 **fail closed**（返回明确错误），不回退宿主 pi；本地开发可显式 `--unsafe-host`，不得用于 token 用户/不可信 skill。

### 验收

- 被测 skill 尝试 `cat /etc/passwd`、`env`、`~/.ssh`、`~/.pi/agent/auth.json`、`curl` 外网、写宿主目录、访问 Docker socket：全部不可得，trace 记录失败。
- 容器内无真实 API key；代理日志无 prompt 正文。
- T3 同一 case 的重复运行仍在同一容器内；T2B 两臂互不影响。
- 超时/超内存/超进程数能杀干净，不残留容器。
- 现有 T2A/T2B/T3 报告在容器 adapter 下指标口径不变。

## 2. 限流

### 维度

- 游客：`IP + se_guest cookie`；IP 单独不可靠（NAT/本机），只做辅助和审计。
- token：`token id`；管理员在 issue 时可给配额。
- 全局：并发 run 数、每小时模型调用数。

### 默认额度（可配置）

| 角色 | 档位 | 每小时 | 每天 | 并发 |
|---|---|---|---|---|
| 游客 | T0 | 20 | 60 | 1 |
| 游客 | T1 | 3 | 10 | 1 |
| token | T0/T1 | 20 | 50 | 1 |
| token | T2A/T2B/T3 | 3 | 8 | 2 |
| 全局 | 所有 | 100 次模型调用 | 500 次 | 4 |

- T1 按 7 次模型调用估算；T2/T3 按 run 计数，代理再按实际模型调用做二级限制。
- 超限返回 429 + `Retry-After`，审计 `rate_limited`；不创建 run 目录。
- 并发限制在启动前检查；`ACTIVE` 里同 actor 的 run 数达到上限即拒绝。
- token 校验失败也限流（例如 30 次/分钟/IP），防枚举。
- 限流状态持久化到 `web/usage.sqlite3`（stdlib sqlite3），事件表按时间窗口计数；启动时清理 30 天前记录。
- 额度写 `web/limits.json`（默认值，gitignore；tracked sample `limits.example.json`），token 记录可带 `daily_runs`/`max_concurrent` 覆盖；管理员 CLI `issue --daily-runs 8 --max-concurrent 2`。
- 游客 T1 被限流后仍可跑 T0；UI 显示“游客 T1 配额已用完，请申请 token”。

### 实现顺序

1. SQLite 计数 + 429（先游客 T1/所有 run 并发）。
2. token 配额 + 管理 CLI 参数。
3. 模型代理二级限流（按实际调用/token）。
4. 容器 adapter（T2/T3 fail closed 到容器可用）。

## 3. 风险与取舍

- 容器每 case 启动有 1–3 秒开销；T3 会放大，但比宿主泄露便宜。
- 默认断网会让需要联网的 skill 在 T2/T3 失败；这是安全默认，联网需求走代理 allowlist，不直接放开。
- Windows 没有 Docker/Podman 时，使用专用 VM/Windows Sandbox；不要回退“cwd + guard”并声称安全。
- T1 不走容器（无 tools、固定 prompt），但仍消耗模型额度，所以限流必须覆盖游客 T1。
