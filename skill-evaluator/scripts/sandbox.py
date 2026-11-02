"""容器隔离后端（阶段 C）：Docker 命令构造、可用性探测、宿主路径重写。

T2/T3 在一次性容器里跑 agent；没有可用容器运行时则 fail closed（capabilities 报缺，
runner 退出 3、Web 412），**绝不回退宿主执行**。本模块只管“怎么跑容器”，不碰指标。

容器边界（见 sandbox/README.md）：
- rootfs 只读；`/work` 只挂当前沙箱副本；`/tmp` 是 tmpfs；不挂宿主 `$HOME`、
  `~/.pi`、云凭据/登录密钥、Docker socket、源码仓库。
- 默认 `--network none`，不把宿主 API key 注入容器（`docker run` 不转发宿主环境变量）。
- 每个 case/arm 一次性容器（`--rm`），不跨 case 复用文件系统。

参数来源：`config.yaml` 的 `sandbox.*`（缺失时用下面默认值；配置四层里代码默认是最低层，
ADR-0035）。不读环境变量。
"""
from __future__ import annotations

import _console

_console.fix()

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import _config

DEFAULT_ENGINE = "docker"
DEFAULT_IMAGE = "skill-evaluator-sandbox:local"   # 与 sandbox/Dockerfile 的构建标签一致
CONTAINER_WORKDIR = "/work"


class SandboxError(RuntimeError):
    """容器后端无法按契约构造命令（缺 workdir 等）；调用方不得退化成宿主执行。"""


@dataclass(frozen=True)
class Limits:
    """容器资源上限；默认值可被 config.yaml 的 sandbox.* 覆盖。"""
    pids: int = 512
    memory: str = "2g"
    cpus: str = "2"
    tmpfs_size: str = "512m"


@dataclass(frozen=True)
class SandboxStatus:
    """一次探测的结果。`reason` 为空 = 可跑；否则是给人看的第一处缺失原因。"""
    engine: str
    executable: str = ""
    client_version: str = ""
    daemon_version: str = ""
    image: str = ""
    image_present: bool = False
    reason: str = ""

    @property
    def available(self) -> bool:
        return not self.reason

    def as_dict(self) -> dict:
        return {"engine": self.engine, "executable": self.executable,
                "client_version": self.client_version, "daemon_version": self.daemon_version,
                "image": self.image, "image_present": self.image_present,
                "available": self.available, "reason": self.reason}

    def summary(self) -> str:
        if self.available:
            return f"{self.engine} {self.daemon_version} 可用；镜像 {self.image} 已就绪"
        return self.reason or "容器运行时不可用"


def _exec(cmd: list, timeout: int = 10) -> tuple:
    """跑一次探测命令：返回 (returncode, stdout, stderr)，失败不抛。"""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


class DockerBackend:
    """Docker 后端：状态探测 + `docker run` 命令构造 + 宿主路径 → `/work` 重写。

    `exec_fn` 可注入假命令执行器（测试没有 Docker 时用），默认真跑 CLI。
    """

    name = "docker"
    workdir = CONTAINER_WORKDIR

    def __init__(self, engine: str | None = None, image: str | None = None,
                 limits: Limits | None = None, exec_fn=None):
        self.engine = (engine or _config.get_str("sandbox.engine", DEFAULT_ENGINE)).strip() \
            or DEFAULT_ENGINE
        self.image = (image or _config.get_str("sandbox.image", DEFAULT_IMAGE)).strip() \
            or DEFAULT_IMAGE
        self.limits = limits or Limits(
            pids=max(16, _config.get_int("sandbox.pids_limit", 512)),
            memory=_config.get_str("sandbox.memory", "2g"),
            cpus=_config.get_str("sandbox.cpus", "2"),
            tmpfs_size=_config.get_str("sandbox.tmpfs_size", "512m"),
        )
        self._exec = exec_fn or _exec

    def executable(self) -> str:
        """CLI 绝对路径；Windows 上 shutil.which 已覆盖 .exe。找不到返回空串。"""
        found = shutil.which(self.engine)
        if not found and os.name == "nt":
            found = shutil.which(self.engine + ".exe")
        return found or ""

    def status(self) -> SandboxStatus:
        """探测 CLI / daemon / 镜像；任何一步缺失都落到 `reason`，不抛异常。"""
        exe = self.executable()
        if not exe:
            return SandboxStatus(
                self.engine, image=self.image,
                reason=(f"缺少 {self.engine} CLI：T2/T3 需要容器运行时"
                        "（安装 Docker Desktop，或在 WSL2 里装 Docker Engine）"))
        code, out, err = self._exec([exe, "version", "--format", "{{.Client.Version}}"])
        client = out.strip().splitlines()[-1][:80] if (code == 0 and out.strip()) else ""
        if not client:
            return SandboxStatus(
                self.engine, executable=exe, image=self.image,
                reason=f"{self.engine} CLI 无法执行：{(err or out).strip()[-200:]}")
        code, out, _ = self._exec([exe, "info", "--format", "{{.ServerVersion}}"])
        daemon = out.strip().splitlines()[-1][:80] if (code == 0 and out.strip()) else ""
        if not daemon:
            return SandboxStatus(
                self.engine, executable=exe, client_version=client, image=self.image,
                reason=f"{self.engine} daemon 不可达：请启动 Docker Desktop / dockerd")
        code, out, _ = self._exec([exe, "image", "inspect", self.image, "--format", "{{.Id}}"])
        present = code == 0 and bool(out.strip())
        reason = "" if present else (
            f"沙箱镜像未构建：docker build -t {self.image} sandbox/（构建后 pin digest）")
        return SandboxStatus(self.engine, executable=exe, client_version=client,
                             daemon_version=daemon, image=self.image,
                             image_present=present, reason=reason)

    def rewrite_arg(self, arg: str, workdir: str | Path, *, prompt: str | None = None) -> str:
        """把绝对宿主路径重写成 `/work/...`；prompt 文本原样保留（只改 argv 的路径）。"""
        if not isinstance(arg, str) or not arg:
            return arg
        if prompt is not None and arg == prompt:
            return arg
        root = str(Path(workdir).resolve()).replace("\\", "/").rstrip("/")
        norm = arg.replace("\\", "/")
        # Windows 路径大小写不敏感；POSIX 保持原样比较，避免误改大小写不同的路径
        probe, base = (norm.lower(), root.lower()) if os.name == "nt" else (norm, root)
        if probe == base:
            return CONTAINER_WORKDIR
        if probe.startswith(base + "/"):
            return f"{CONTAINER_WORKDIR}/{norm[len(root) + 1:]}"
        return arg

    def rewrite_path(self, path: str | Path, workdir: str | Path) -> str:
        return self.rewrite_arg(str(path), workdir)

    def rewrite_argv(self, argv: list, workdir: str | Path, *, prompt: str | None = None) -> list:
        return [self.rewrite_arg(item, workdir, prompt=prompt) for item in argv]

    def wrap(self, argv: list, workdir: str | Path, *, prompt: str | None = None) -> list:
        """把 agent 命令包成 `docker run ... <image> <argv>`（workdir 挂到 /work）。"""
        root = Path(workdir).resolve()
        if not root.is_dir():
            raise SandboxError(f"容器 workdir 不存在: {workdir}")
        lim = self.limits
        run = [
            self.executable() or self.engine, "run",
            "--rm",                       # 一次性容器：case 结束即销毁
            "--read-only",                # rootfs 只读；只有 /work 与 tmpfs 可写
            "--network", "none",          # 无公网；模型访问需宿主代理（未实现）
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(lim.pids),
            "--memory", lim.memory,
            "--cpus", lim.cpus,
            "--tmpfs", f"/tmp:rw,noexec,nosuid,size={lim.tmpfs_size}",
            "--workdir", CONTAINER_WORKDIR,
            # 只挂当前沙箱副本；不挂 $HOME / ~/.pi / docker.sock / 源码仓库
            "--mount", f"type=bind,src={root},dst={CONTAINER_WORKDIR}",
            self.image,
        ]
        # Linux 上用宿主 uid/gid 运行：仍是非 root，但能写 bind mount（/work）。
        # Windows 没有 os.getuid；Docker Desktop 的 bind mount 由 VM 处理文件权限。
        if os.name != "nt" and hasattr(os, "getuid"):
            run[run.index("--workdir"):run.index("--workdir")] = [
                "--user", f"{os.getuid()}:{os.getgid()}"]
        return run + self.rewrite_argv(argv, root, prompt=prompt)
