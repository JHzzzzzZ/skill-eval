"""能力矩阵（阶段 C）：按 tier + adapter + isolation 回答“这一档能不能跑、缺什么”。

同一份判定供 CLI（runner 退出码 3）与 Web（`GET /api/capabilities`、`POST /api/run` 412）
共用，避免两处各写一套依赖条件。

分层原则（设计见 docs/design/sandbox-and-rate-limit.md §3）：
- T0/T1 不要求容器：缺 Docker 不能把游客卡死。
- T2/T3 要求容器运行时 + 沙箱镜像 + 可用执行载体；默认 docker 缺失即 fail closed。
- 缺容器但 adapter 可用时给出 `fallback`（宿主临时目录）：Web 必须由用户显式确认后才走，
  CLI 必须显式 `--isolation host`；两者都不是安全边界，也不会静默发生。
- 模型代理未实现：容器默认断网，提示里明说，不静默用宿主执行或宿主 key 兜底。
"""
from __future__ import annotations

import _console

_console.fix()

import adapter_registry
import sandbox

TIERS = ("t0", "t1", "t2a", "t2b", "t3")
CONTAINER_TIERS = ("t2a", "t2b", "t3")
CONTAINER_ISOLATION = "docker"
MODEL_PROXY_NOTE = ("模型代理尚未实现：容器默认 --network none，T2/T3 的模型调用需要宿主代理；"
                    "不会改用宿主执行或注入宿主 API key")
HOST_ISOLATION_WARNING = ("宿主临时目录，不是安全边界：被测 skill 仍可能读到宿主文件、访问网络；"
                          "只在确认可信任时使用")


class CapabilityError(RuntimeError):
    """该档所需能力缺失；调用方必须 fail closed（runner 退出码 3 / Web 412）。"""


def requires_container(tier: str) -> bool:
    return str(tier).lower() in CONTAINER_TIERS


def default_isolation(tier: str) -> str:
    """T2+ 默认容器；T0/T1 在宿主跑静态检查与 LLM 评审。"""
    return CONTAINER_ISOLATION if requires_container(tier) else "host"


def adapter_info(name: str | None) -> dict:
    """执行载体是否可用；未知名字明确报错，不回退默认载体。"""
    key = (name or adapter_registry.DEFAULT).strip() or adapter_registry.DEFAULT
    try:
        adapter_registry.get(key)
    except adapter_registry.UnknownAdapterError as exc:
        return {"name": key, "available": False, "reason": str(exc)}
    if adapter_registry.is_available(key):
        return {"name": key, "available": True, "reason": ""}
    return {"name": key, "available": False,
            "reason": f"执行载体 {key} 不可用：本机找不到它的 CLI"}


def check(tier: str, *, adapter: str | None = None, isolation: str | None = None,
          sandbox_status=None) -> dict:
    """判定一档能否运行。返回 ok / missing（机器可读）/ reasons+fix（给人看）。"""
    tier = str(tier).lower()
    if tier not in TIERS:
        raise ValueError(f"未知 tier：{tier}；可用：{', '.join(TIERS)}")
    iso = str(isolation or default_isolation(tier)).lower()
    if iso not in ("host", "docker"):
        raise ValueError(f"未知 isolation：{iso}；可用：host, docker")
    result = {"tier": tier, "isolation": iso, "ok": True, "missing": [], "reasons": [],
              "fix": [], "warnings": [], "container": None, "fallback": None,
              "adapter": adapter_info(adapter)}
    if not requires_container(tier):
        # T0/T1 不需要容器，也不经过执行载体（T1 直连 LLM）：缺 Docker 不影响这一档。
        return result
    if iso == "host":
        # 显式宿主执行：CLI 传 --isolation host，或 Web 用户确认回退后传 host。
        # 跳过容器不等于跳过 adapter 检查——宿主执行同样要有可用的 agent CLI。
        _gate_adapter(result, in_container=False)
        result["warnings"].append(HOST_ISOLATION_WARNING)
        result["ok"] = not result["missing"]
        return result

    status = sandbox_status or sandbox.DockerBackend().status()
    result["container"] = status.as_dict()
    if not status.executable:
        result["missing"].append("docker")
        result["reasons"].append(status.reason)
        result["fix"].append("安装 Docker Desktop，或在 WSL2 里安装 Docker Engine")
    elif not status.daemon_version:
        result["missing"].append("docker_daemon")
        result["reasons"].append(status.reason)
        result["fix"].append("启动 Docker Desktop / dockerd 后重试")
    elif not status.image_present:
        result["missing"].append("docker_image")
        result["reasons"].append(status.reason)
        result["fix"].append(f"docker build -t {status.image} sandbox/")
    _gate_adapter(result, in_container=True)
    result["warnings"].append(MODEL_PROXY_NOTE)
    result["ok"] = not result["missing"]
    result["fallback"] = _host_fallback(result)
    return result


def _gate_adapter(result: dict, *, in_container: bool) -> None:
    """adapter 是两种隔离共同的硬依赖：宿主执行同样需要 agent CLI。"""
    if result["adapter"]["available"]:
        return
    result["missing"].append("adapter")
    result["reasons"].append(result["adapter"]["reason"])
    result["fix"].append("安装执行载体 CLI（容器内与宿主执行都需要，见 sandbox/README.md）"
                         if in_container else "安装执行载体 CLI（宿主回退同样需要）")


def _host_fallback(result: dict) -> dict:
    """缺容器时是否允许用户确认后改走宿主临时目录；缺 adapter 则连回退也跑不起来。"""
    if not result["adapter"]["available"]:
        return {"isolation": "host", "available": False,
                "reason": "缺执行载体 CLI：容器与宿主回退都无法运行"}
    container_missing = [item for item in result["missing"] if item.startswith("docker")]
    if not container_missing:
        return {"isolation": "host", "available": False, "reason": "容器可用，不需要宿主回退"}
    return {"isolation": "host", "available": True,
            "reason": "没有可用容器运行时：确认后可在宿主临时目录执行，隔离较弱"}


def require(tier: str, *, adapter: str | None = None, isolation: str | None = None,
            sandbox_status=None) -> dict:
    """check() 的抛错版本：缺失时抛 CapabilityError（runner 映射为退出码 3）。"""
    result = check(tier, adapter=adapter, isolation=isolation, sandbox_status=sandbox_status)
    if not result["ok"]:
        message = "缺少运行该评估深度所需能力：" + "；".join(result["reasons"])
        if result["fix"]:
            message += "。修复：" + "；".join(result["fix"])
        raise CapabilityError(message)
    return result


def adapter_infos() -> list:
    """所有已注册 adapter 的可用性（UI 下拉与依赖提示用；探测失败不抛）。"""
    return [{"name": name, **adapter_info(name)} for name in adapter_registry.names()]


def snapshot(*, adapter: str | None = None, sandbox_status=None) -> dict:
    """全档能力快照（`GET /api/capabilities` 用）：一次探测，逐档复用。"""
    status = sandbox_status or sandbox.DockerBackend().status()
    return {"tiers": {tier: check(tier, adapter=adapter, sandbox_status=status)
                      for tier in TIERS},
            "container": status.as_dict(),
            "adapter": adapter_info(adapter),
            "adapters": adapter_infos(),
            "model_proxy": {"configured": False, "note": MODEL_PROXY_NOTE}}
