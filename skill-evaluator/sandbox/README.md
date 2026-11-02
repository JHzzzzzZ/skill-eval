# T2/T3 沙箱镜像与容器边界

`scripts/sandbox.py` 用这个镜像给 T2/T3 的每个 case/arm 起一次性容器；**没有可用容器运行时**
时不会静默回退：页面点运行会先弹确认，确认后以 `--isolation host` 在宿主临时目录执行；
CLI 则必须显式传 `--isolation host`。宿主临时目录只是 cwd 副本，不是安全边界。T0/T1 不经过容器。

## 构建

```bash
cd skill-evaluator
docker build -t skill-evaluator-sandbox:local sandbox/
```

镜像名与 `scripts/sandbox.py` 的 `DEFAULT_IMAGE` 一致；`config.yaml` 可用
`sandbox.image` 覆盖（缺失时用代码默认，ADR-0035）。生产环境请把
`node:22-bookworm-slim` 换成 digest，并把 `PI_VERSION` / `CLAUDE_CODE_VERSION`
锁到精确版本后重新 pin。

自检：

```bash
bash scripts/check-deps.sh                 # 基础依赖（T0/T1）
bash scripts/check-deps.sh --tier t2a      # 追加 Docker daemon + 镜像；缺失仅 [warn]（可确认后宿主回退）
docker image inspect skill-evaluator-sandbox:local
```

## 容器参数（由 `sandbox.py` 生成，不由调用方拼）

| 参数 | 作用 |
|---|---|
| `--rm` | 每个 case/arm 一次性容器，不跨 case 复用文件系统 |
| `--read-only` | rootfs 只读 |
| `--network none` | 无公网；模型访问必须走宿主代理（见下） |
| `--cap-drop ALL` + `--security-opt no-new-privileges` | 去掉 Linux capabilities，禁止提权 |
| `--pids-limit` / `--memory` / `--cpus` | 进程数、内存、CPU 上限（`config.yaml` 的 `sandbox.*` 可覆盖） |
| `--tmpfs /tmp:rw,noexec,nosuid,size=512m` | 唯一的内存临时盘 |
| `--workdir /work` + `--mount type=bind,src=<沙箱副本>,dst=/work` | 只挂当前 skill 副本 |
| 镜像默认非 root 用户（uid 10001） | 容器内不拥有宿主权限 |

**不挂载**宿主 `$HOME`、`~/.pi`、云凭据 / 登录密钥、Docker socket、源码仓库；agent 命令里的
宿主绝对路径在 `argv` 里重写为 `/work/...`（prompt 文本不改）。宿主写入检测（guard）在
容器模式下不适用，trace 里会明确标 `guard.applicable=false`。

## 模型访问（未实现）

设计上容器只拿到一个短期代理 token，宿主侧代理注入真实 key 并限流；**本轮没有实现
`scripts/model_proxy.py`**。因此：

- 容器内拿不到真实 API key，也不会把宿主环境变量传进去；
- `--network none` 下需要模型调用的 skill 在 T2/T3 会失败——这是安全默认，不是 bug；
- `capabilities.snapshot()` / `GET /api/capabilities` 会显式返回
  `model_proxy.configured = false` 与原因，不会静默改用宿主执行。

代理落地前，T2/T3 只适合不依赖模型、或在容器内自带模型访问配置的被测 skill。

## 运行

```bash
python scripts/runner.py --skill <skill目录> --tier t2a          # 默认 --isolation docker
python scripts/runner.py --skill <skill目录> --tier t2a --isolation host   # 显式宿主执行
python scripts/trace_run.py <沙箱目录> "<prompt>" --skill <沙箱目录> --isolation docker
```

没有容器时：CLI 必须显式加 `--isolation host`（runner 不会自动回退）；页面点运行时弹出确认，
用户确认后才发出 `allow_unsafe_host: true`。宿主临时目录（`results/sandbox`）只是 cwd 副本，
**不是安全边界**：被测 skill 仍可能读到宿主文件、访问网络，只在可信任时使用。

注意：能力检查（`capabilities.py`）探测的是**宿主**的执行载体 CLI；容器内也需要同名 CLI，
因此 T2/T3 要求两边都有可探测的 `pi`（或所选 adapter）。
