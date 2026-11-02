"""scripts/capabilities.py 的分层能力矩阵 + runner 退出码 3（无 Docker，全部 fake）。"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import capabilities  # noqa: E402
import runner  # noqa: E402
import sandbox  # noqa: E402


def status(available=True, reason="", image_present=True):
    if available:
        return sandbox.SandboxStatus("docker", executable="/usr/bin/docker",
                                     client_version="27.0.0", daemon_version="27.0.0",
                                     image="test-image:local", image_present=image_present)
    return sandbox.SandboxStatus("docker", image="test-image:local", reason=reason)


@pytest.fixture
def adapter_ok(monkeypatch):
    monkeypatch.setattr(capabilities.adapter_registry, "is_available",
                        lambda name: name == "pi")


def test_t0_t1_do_not_require_container(adapter_ok):
    for tier in ("t0", "t1"):
        result = capabilities.check(tier, sandbox_status=status(False, "缺少 docker CLI"))
        assert result["ok"] is True, result
        assert result["missing"] == [] and result["isolation"] == "host"


def test_container_tiers_fail_closed_without_docker(adapter_ok):
    for tier in ("t2a", "t2b", "t3"):
        result = capabilities.check(tier, sandbox_status=status(False, "缺少 docker CLI：需要容器运行时"))
        assert result["ok"] is False
        assert result["missing"] == ["docker"]
        assert result["isolation"] == "docker"
        assert "容器运行时" in result["reasons"][0]
        assert result["fix"]


def test_container_tier_requires_image(adapter_ok):
    no_image = sandbox.SandboxStatus("docker", executable="/usr/bin/docker",
                                     client_version="27.0.0", daemon_version="27.0.0",
                                     image="test-image:local", image_present=False,
                                     reason="沙箱镜像未构建")
    result = capabilities.check("t2a", sandbox_status=no_image)
    assert result["ok"] is False and result["missing"] == ["docker_image"]


def test_container_tier_requires_adapter(monkeypatch):
    monkeypatch.setattr(capabilities.adapter_registry, "is_available", lambda name: False)
    result = capabilities.check("t2a", sandbox_status=status(True))
    assert result["ok"] is False and "adapter" in result["missing"]


def test_container_tier_ok_with_docker_and_adapter(adapter_ok):
    result = capabilities.check("t3", adapter="pi", sandbox_status=status(True))
    assert result["ok"] is True, result
    assert result["missing"] == [] and result["container"]["image_present"] is True
    # 模型代理未实现必须显式提示，不能静默降级宿主
    assert any("模型代理" in warning for warning in result["warnings"])


def test_explicit_host_isolation_is_dev_only(adapter_ok):
    result = capabilities.check("t2a", isolation="host", sandbox_status=status(False, "no docker"))
    assert result["ok"] is True
    assert any("仅供本地开发" in warning for warning in result["warnings"])


def test_unknown_tier_or_isolation_is_value_error(adapter_ok):
    with pytest.raises(ValueError):
        capabilities.check("t9")
    with pytest.raises(ValueError):
        capabilities.check("t0", isolation="vm")


def test_default_isolation_per_tier():
    assert capabilities.default_isolation("t0") == "host"
    assert capabilities.default_isolation("t1") == "host"
    for tier in ("t2a", "t2b", "t3"):
        assert capabilities.default_isolation(tier) == "docker"


def test_snapshot_covers_all_tiers(adapter_ok):
    payload = capabilities.snapshot(sandbox_status=status(True))
    assert set(payload["tiers"]) == set(capabilities.TIERS)
    assert payload["tiers"]["t0"]["ok"] is True
    assert payload["tiers"]["t2a"]["ok"] is True
    assert payload["container"]["daemon_version"] == "27.0.0"
    assert payload["model_proxy"]["configured"] is False
    assert "模型代理" in payload["model_proxy"]["note"]
    names = {item["name"] for item in payload["adapters"]}
    assert {"pi", "claude-code"} <= names
    assert any(item["name"] == "pi" and item["available"] for item in payload["adapters"])


def make_skill(tmp_path: Path) -> Path:
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nbody\n",
                                    encoding="utf-8")
    return skill


def deny_check(*_args, **_kwargs):
    return {"tier": "t2a", "isolation": "docker", "ok": False,
            "missing": ["docker"], "reasons": ["缺少 docker CLI"],
            "fix": ["安装 Docker"], "warnings": []}


def test_runner_refuses_container_tier_without_capability(tmp_path, monkeypatch):
    skill = make_skill(tmp_path)
    results = tmp_path / "results"
    monkeypatch.setattr(capabilities, "check", deny_check)
    args = SimpleNamespace(tier="t2a", skill=str(skill), results=str(results),
                           progress=None, dry_run=False, isolation=None,
                           harness="pi", version="v-test")
    with pytest.raises(capabilities.CapabilityError, match="缺少 docker CLI"):
        runner.run(args)
    assert not results.exists()          # 失败不建 results、不写阶段产物


def test_runner_main_exits_3_when_capability_missing(tmp_path, monkeypatch):
    skill = make_skill(tmp_path)
    results = tmp_path / "results"
    monkeypatch.setattr(capabilities, "check", deny_check)
    monkeypatch.setattr(sys, "argv", ["runner.py", "--skill", str(skill),
                                      "--tier", "t3", "--results", str(results)])
    assert runner.main() == 3
    assert not results.exists()


def test_runner_t0_never_checks_container(tmp_path, monkeypatch):
    skill = make_skill(tmp_path)
    calls = []
    monkeypatch.setattr(capabilities, "check", lambda *a, **k: calls.append(a) or deny_check())
    args = SimpleNamespace(tier="t0", skill=str(skill), results=str(tmp_path / "r"),
                           progress=None, dry_run=True, isolation=None, harness="pi")
    assert runner.run(args) == 0
    assert calls == []
