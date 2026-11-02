"""访问 token 的持久化与管理员 CLI，以及本地审计文件写入。

服务端只存 `sha256(token)`；明文只在 `issue` 时返回/打印一次。不提供网页注册：
申请由人维护，审批后管理员执行 `issue`。

用法（`--tokens-file` / `--audit-file` 是全局参数，写在子命令前，默认
`web/tokens.json` / `web/audit.jsonl`）：
    python web/token_admin.py issue --name "张三" [--note "申请表 #12"]
    python web/token_admin.py list
    python web/token_admin.py revoke --id tok_ab12cd34
    python web/token_admin.py --tokens-file /tmp/t.json --audit-file /tmp/a.jsonl list
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from pathlib import Path

WEB = Path(__file__).resolve().parent
TOKENS_FILE = WEB / "tokens.json"
AUDIT_FILE = WEB / "audit.jsonl"
TOKEN_PREFIX = "se_"
TOKEN_BYTES = 32
PUBLIC_FIELDS = ("id", "name", "note", "created_at", "active")


def _digest(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _read(path=None) -> list:
    path = Path(path) if path is not None else Path(TOKENS_FILE)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    records = data.get("tokens") if isinstance(data, dict) else data
    return [r for r in records if isinstance(r, dict)] if isinstance(records, list) else []


def has_tokens(path=None) -> bool:
    """token 库里是否有任何记录（含已停用）：有就说明已进入多用户模式。

    文件存在但损坏时按“已进入多用户模式”处理（fail closed），避免 legacy run
    在 token 库损坏时对所有人放行。
    """
    path = Path(path) if path is not None else Path(TOKENS_FILE)
    if not path.is_file():
        return False
    if _read(path):
        return True
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    return False


def _write(records: list, path=None) -> None:
    """临时文件 + replace，避免半截写坏 token 库；POSIX 尽量 0600。"""
    path = Path(path) if path is not None else Path(TOKENS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"tokens": records}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _public(record: dict) -> dict:
    return {key: record.get(key) for key in PUBLIC_FIELDS}


def issue(name: str, note: str = "", path=None) -> dict:
    """签发一个持久 token；返回值里带明文 `token`，落盘记录里没有。"""
    name = str(name or "").strip()
    if not name:
        raise ValueError("token 必须有名字（申请人 / 用途）")
    records = _read(path)
    token = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    record = {"id": "tok_" + secrets.token_hex(4), "name": name, "note": str(note or ""),
              "hash": _digest(token),
              "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "active": True}
    records.append(record)
    _write(records, path)
    return {**_public(record), "token": token}


def list_tokens(path=None) -> list:
    """列出 token 的公开字段；不含 hash，也不含明文。"""
    return [_public(r) for r in _read(path)]


def revoke(token_id: str, path=None) -> dict:
    """停用（不删除）一个 token，下次校验立即失败。"""
    records = _read(path)
    for record in records:
        if record.get("id") == token_id:
            record["active"] = False
            record["revoked_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            _write(records, path)
            return _public(record)
    raise KeyError(f"没有这个 token：{token_id}")


def verify(token: str, path=None) -> dict | None:
    """校验明文 token。未知/停用/格式不对一律返回 None，调用方统一 401。"""
    token = str(token or "")
    if not token.startswith(TOKEN_PREFIX):
        return None
    digest = _digest(token)
    hit = None
    for record in _read(path):
        candidate = str(record.get("hash") or "")
        if record.get("active") and candidate and hmac.compare_digest(candidate, digest):
            hit = _public(record)
    return hit


def append_audit(record: dict, path=None) -> None:
    """追加一行审计。写失败只告警到 stderr，不阻断主流程。"""
    path = Path(path) if path is not None else Path(AUDIT_FILE)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        print(f"审计写入失败：{exc}", file=sys.stderr)


def _audit_cli(action: str, detail: str, *, path=None, status: int = 200) -> None:
    append_audit({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "ip": "", "x_forwarded_for": "",
                  "user_agent": "", "actor_kind": "admin", "actor_id": "",
                  "actor_name": "cli", "action": action, "method": "CLI",
                  "path": "web/token_admin.py", "status": status, "run_id": "",
                  "tier": "", "skill_sha256": "", "detail": detail}, path=path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="管理 web 访问 token（T2/T3 门禁）")
    parser.add_argument("--tokens-file", default=str(TOKENS_FILE), metavar="PATH",
                        help="token 库路径（默认 web/tokens.json）")
    parser.add_argument("--audit-file", default=str(AUDIT_FILE), metavar="PATH",
                        help="审计文件路径（默认 web/audit.jsonl）")
    sub = parser.add_subparsers(dest="command", required=True)
    p_issue = sub.add_parser("issue", help="签发 token（明文只显示一次）")
    p_issue.add_argument("--name", required=True, help="申请人 / 用途")
    p_issue.add_argument("--note", default="", help="备注，如申请表编号")
    sub.add_parser("list", help="列出 token（不含 hash 与明文）")
    p_revoke = sub.add_parser("revoke", help="停用 token")
    p_revoke.add_argument("--id", required=True)
    ns = parser.parse_args(argv)
    tokens = Path(ns.tokens_file)
    audit = Path(ns.audit_file)

    if ns.command == "issue":
        record = issue(ns.name, ns.note, path=tokens)
        _audit_cli("token_issue", f"{record['id']} {record['name']}", path=audit)
        print(f"id: {record['id']}")
        print(f"name: {record['name']}")
        print(f"token: {record['token']}")
        print("明文只显示这一次；服务端只存 hash。", file=sys.stderr)
        return 0
    if ns.command == "list":
        rows = list_tokens(path=tokens)
        if not rows:
            print(f"没有 token（{tokens} 不存在或为空）")
            return 0
        for row in rows:
            state = "active" if row["active"] else "revoked"
            print(f"{row['id']}\t{state}\t{row['name']}\t{row['created_at']}\t{row['note']}")
        return 0
    try:
        record = revoke(ns.id, path=tokens)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _audit_cli("token_revoke", record["id"], path=audit)
    print(f"revoked: {record['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
