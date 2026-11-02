"""scripts/sandbox.py 的容器命令/路径重写/状态探测（无 Docker 环境：全部 fake）。"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import sandbox  # noqa: E402
import trace_run  # noqa: E402

TRACE_RUN = SCRIPTS / "trace_run.py"


def fake_exec(cli="27.0.0", daemon="27.0.0", image="sha256:abc123"):
    """假 docker CLI：按子命令返回固定输出；daemon/image 传空串 = 不可用。"""
    def run(cmd, timeout=10):
        if "--format" in cmd and "version" in cmd:
            return (0, f"{cli}\n", "") if cli else (1, "", "no cli")
        if "info" in cmd:
            return (0, f"{daemon}\n", "") if daemon else (1, "", "cannot connect to daemon")
        if "image" in cmd and "inspect" in cmd:
            return (0, f"{image}\n", "") if image else (1, "", "no such image")
        return 1, "", f"unexpected: {cmd}"
    return run


def backend(tmp_path, **kwargs):
    return sandbox.DockerBackend(engine="docker", image="test-image:local",
                                 exec_fn=fake_exec(**kwargs))


def test_status_reports_missing_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    status = backend(tmp_path).status()
    assert status.available is False
    assert "CLI" in status.reason and "容器运行时" in status.summary()


def test_status_requires_daemon(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/docker")
    status = backend(tmp_path, daemon="").status()
    assert status.available is False
    assert "daemon" in status.reason
    assert status.client_version


def test_status_requires_image(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/docker")
    status = backend(tmp_path, image="").status()
    assert status.available is False
    assert "镜像未构建" in status.reason
    assert "docker build -t test-image:local sandbox/" in status.reason


def test_status_available_and_serializable(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/docker")
    status = backend(tmp_path).status()
    assert status.available is True
    payload = status.as_dict()
    assert payload["daemon_version"] == "27.0.0"
    assert payload["image_present"] is True
    assert payload["available"] is True


def _value(argv, flag):
    return argv[argv.index(flag) + 1]


def test_wrap_locks_down_container(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/docker")
    work = tmp_path / "work"
    work.mkdir()
    argv = backend(tmp_path).wrap(["pi", "--skill", str(work), "--", "hello"], work)
    assert argv[0] == "/usr/bin/docker" and argv[1] == "run"
    assert argv[2:4] == ["--rm", "--read-only"]
    assert _value(argv, "--network") == "none"
    assert _value(argv, "--cap-drop") == "ALL"
    assert _value(argv, "--security-opt") == "no-new-privileges"
    assert int(_value(argv, "--pids-limit")) > 0
    assert _value(argv, "--memory") and _value(argv, "--cpus")
    assert _value(argv, "--tmpfs").startswith("/tmp:")
    assert _value(argv, "--workdir") == "/work"
    mount = _value(argv, "--mount")
    assert "dst=/work" in mount and str(work.resolve()) in mount
    assert argv[argv.index("test-image:local") + 1:] == ["pi", "--skill", "/work", "--", "hello"]


def test_wrap_mounts_nothing_from_host_home_or_docker_socket(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/docker")
    work = tmp_path / "work"
    work.mkdir()
    argv = backend(tmp_path).wrap(["pi", "--", "hi"], work)
    joined = " ".join(argv)
    assert "docker.sock" not in joined
    assert ".ssh" not in joined and ".pi" not in joined
    mounts = [argv[i + 1] for i, item in enumerate(argv) if item == "--mount"]
    assert len(mounts) == 1                                   # 只有 workdir 一个挂载
    assert str(work.resolve()) in mounts[0]


def test_rewrite_only_rewrites_argv_not_prompt(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    skill = work / "SKILL.md"
    skill.write_text("x", encoding="utf-8")
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/docker")
    prompt = f"读 {skill} 并总结"
    argv = backend(tmp_path).wrap(["pi", "--skill", str(work), "--", prompt], work, prompt=prompt)
    command = argv[argv.index("test-image:local") + 1:]   # 镜像之后的 agent 命令
    assert str(work.resolve()) not in " ".join(command[:-1])
    assert argv[-1] == prompt                      # prompt 文本原样保留
    assert "/work/SKILL.md" not in prompt          # 不反向改写 prompt 内容


def test_rewrite_path_handles_children_and_outsiders(tmp_path):
    work = tmp_path / "work"
    (work / "sub").mkdir(parents=True)
    be = sandbox.DockerBackend(engine="docker", exec_fn=fake_exec())
    assert be.rewrite_path(work, work) == "/work"
    assert be.rewrite_path(work / "sub" / "a.txt", work) == "/work/sub/a.txt"
    outside = tmp_path / "other.txt"
    assert be.rewrite_path(outside, work) == str(outside)


def test_wrap_rejects_missing_workdir(tmp_path):
    be = sandbox.DockerBackend(engine="docker", exec_fn=fake_exec())
    with pytest.raises(sandbox.SandboxError):
        be.wrap(["pi"], tmp_path / "missing")


def test_trace_run_build_only_wraps_docker_command(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    r = subprocess.run([sys.executable, str(TRACE_RUN), str(work), "hello world",
                        "--isolation", "docker", "--build-only", "--skill", str(work)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    command = json.loads(r.stdout)["command"]
    assert command[1] == "run"
    assert "--network" in command and "--read-only" in command
    assert command[-1] == "hello world"            # prompt 不重写
    assert _value(command, "--workdir") == "/work"


def test_trace_run_docker_fails_closed_without_container(tmp_path, monkeypatch, capsys):
    work = tmp_path / "work"
    work.mkdir()
    pi = trace_run.adapter_registry.get("pi")
    monkeypatch.setattr(pi, "available", lambda: (True, "test"))
    fake_status = sandbox.SandboxStatus("docker", image="img",
                                        reason="缺少 docker CLI：T2/T3 需要容器运行时")
    monkeypatch.setattr(trace_run.sandbox.DockerBackend, "status",
                        lambda self: fake_status)
    monkeypatch.setattr(sys, "argv", ["trace_run.py", str(work), "hi",
                                      "--isolation", "docker", "--skill", str(work)])
    trace_run.main()
    trace = json.loads(capsys.readouterr().out)
    assert trace["passed"] is False and trace["triggered"] is False
    assert "容器不可用" in trace["errors"][0]
    assert "不回退宿主" in trace["errors"][0]


def test_trace_run_rejects_unknown_isolation(tmp_path):
    r = subprocess.run([sys.executable, str(TRACE_RUN), str(tmp_path), "hi",
                        "--isolation", "vm", "--build-only"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 2
    assert "--isolation" in r.stderr


def test_trace_run_docker_records_isolation_and_skips_host_guard(tmp_path, monkeypatch, capsys):
    work = tmp_path / "work"
    work.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    pi = trace_run.adapter_registry.get("pi")
    monkeypatch.setattr(pi, "available", lambda: (True, "test"))
    monkeypatch.setattr(trace_run.sandbox.DockerBackend, "status", lambda self: sandbox.SandboxStatus(
        "docker", executable="/usr/bin/docker", daemon_version="27.0.0",
        image="test-image:local", image_present=True))

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(trace_run.subprocess, "run", lambda *a, **k: _Done())
    monkeypatch.setattr(sys, "argv", ["trace_run.py", str(work), "hi", "--isolation", "docker",
                                      "--skill", str(work), "--guard-repo", str(repo)])
    trace_run.main()
    trace = json.loads(capsys.readouterr().out)
    assert trace["host"]["isolation"] == "docker"
    assert trace["host"]["sandbox"]["image"] == "test-image:local"
    assert trace["guard"]["applicable"] is False
    assert "docker 隔离" in trace["guard"]["reason"]
