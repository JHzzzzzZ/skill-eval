"""Seam: python web/token_admin.py <issue|list|revoke> -> stdout / tokens.json

输入：全局 `--tokens-file` / `--audit-file`（子命令前）+ `--name`（必填）/ `--note` / `--id`。
输出：`issue` 打印 id/name/token（明文只此一次）；`list` 每行一条公开字段；`revoke` 打印 revoked。
契约：落盘只存 sha256(token)；list 与审计都不出现 hash 或明文；停用后 verify 立即失败。
路径只来自模块常量或 CLI 参数，不新增环境变量。
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

TOKEN_ADMIN = Path(__file__).parent.parent / "web" / "token_admin.py"
spec = importlib.util.spec_from_file_location("token_admin_mod", TOKEN_ADMIN)
admin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admin)


def store(tmp_path: Path) -> Path:
    return tmp_path / "tokens.json"


def test_issue_returns_plaintext_once_and_stores_only_hash(tmp_path):
    path = store(tmp_path)
    record = admin.issue("张三", "申请表 #12", path=path)
    assert record["token"].startswith("se_")
    assert record["name"] == "张三" and record["active"] is True

    raw = path.read_text(encoding="utf-8")
    saved = json.loads(raw)["tokens"][0]
    assert set(saved) == {"id", "name", "note", "hash", "created_at", "active"}
    assert saved["hash"] == admin._digest(record["token"])
    assert record["token"] not in raw          # 明文不落盘
    assert "hash" not in admin.list_tokens(path=path)[0]


def test_issue_requires_name(tmp_path):
    with pytest.raises(ValueError, match="名字"):
        admin.issue("  ", path=store(tmp_path))


def test_verify_accepts_active_and_rejects_unknown_or_malformed(tmp_path):
    path = store(tmp_path)
    record = admin.issue("李四", path=path)
    assert admin.verify(record["token"], path=path)["id"] == record["id"]
    assert admin.verify(record["token"] + "x", path=path) is None
    assert admin.verify("", path=path) is None
    assert admin.verify("not-a-token", path=path) is None
    assert admin.verify(record["token"], path=tmp_path / "missing.json") is None


def test_revoke_disables_token_immediately(tmp_path):
    path = store(tmp_path)
    record = admin.issue("王五", path=path)
    again = admin.issue("赵六", path=path)
    admin.revoke(record["id"], path=path)
    assert admin.verify(record["token"], path=path) is None
    assert admin.verify(again["token"], path=path)["name"] == "赵六"     # 只停用指定 token
    states = {r["id"]: r["active"] for r in admin.list_tokens(path=path)}
    assert states == {record["id"]: False, again["id"]: True}
    with pytest.raises(KeyError):
        admin.revoke("tok_missing", path=path)


def test_store_path_comes_from_module_not_environment(tmp_path, monkeypatch):
    """路径只由模块常量 / CLI 参数决定：新增 env 会让部署与测试静默分叉。"""
    monkeypatch.setattr(admin, "TOKENS_FILE", tmp_path / "tokens.json")
    monkeypatch.setattr(admin, "AUDIT_FILE", tmp_path / "audit.jsonl")
    monkeypatch.setenv("SKILL_EVAL_TOKENS_FILE", str(tmp_path / "ignored.json"))
    admin.issue("张三")
    assert (tmp_path / "tokens.json").is_file()
    assert not (tmp_path / "ignored.json").exists()


def test_has_tokens_distinguishes_empty_store(tmp_path):
    path = store(tmp_path)
    assert admin.has_tokens(path=path) is False
    record = admin.issue("张三", path=path)
    assert admin.has_tokens(path=path) is True
    admin.revoke(record["id"], path=path)
    assert admin.has_tokens(path=path) is True     # 停用仍是“已启用多用户模式”


def test_cli_issue_list_revoke(tmp_path):
    tokens = store(tmp_path)
    audit = tmp_path / "audit.jsonl"

    def run(*args):
        return subprocess.run([sys.executable, str(TOKEN_ADMIN), "--tokens-file", str(tokens),
                               "--audit-file", str(audit), *args],
                              capture_output=True, text=True, encoding="utf-8")

    issued = run("issue", "--name", "张三", "--note", "申请表 #12")
    assert issued.returncode == 0, issued.stderr
    fields = dict(line.split(": ", 1) for line in issued.stdout.splitlines() if ": " in line)
    token = fields["token"]
    assert token.startswith("se_") and token not in tokens.read_text(encoding="utf-8")

    listed = run("list")
    assert listed.returncode == 0
    assert fields["id"] in listed.stdout and "active" in listed.stdout
    assert token not in listed.stdout and "hash" not in listed.stdout

    revoked = run("revoke", "--id", fields["id"])
    assert revoked.returncode == 0, revoked.stderr
    assert admin.verify(token, path=tokens) is None
    assert "revoked" in run("list").stdout
    assert run("revoke", "--id", "tok_missing").returncode == 1

    actions = [json.loads(line)["action"] for line in audit.read_text(encoding="utf-8").splitlines()]
    assert actions == ["token_issue", "token_revoke"]
    assert token not in audit.read_text(encoding="utf-8")
    assert all(json.loads(line)["actor_kind"] == "admin"
               for line in audit.read_text(encoding="utf-8").splitlines())


def test_audit_write_failure_does_not_raise(tmp_path, capsys):
    blocked = tmp_path / "audit-dir"
    blocked.mkdir()
    admin.append_audit({"action": "x"}, path=blocked)     # 目录不能当文件追加
    assert "审计写入失败" in capsys.readouterr().err


def test_has_tokens_fails_closed_on_corrupt_store(tmp_path):
    path = store(tmp_path)
    path.write_text("{not json", encoding="utf-8")
    assert admin.has_tokens(path) is True       # 损坏按“已有 token”处理
    path.write_text('{"tokens": []}', encoding="utf-8")
    assert admin.has_tokens(path) is False      # 合法空库才回到单用户迁移期
