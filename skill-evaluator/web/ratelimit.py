"""运行配额：自然窗口计数 + 并发上限，stdlib sqlite3。

- 游客按 `se_guest` cookie 与 IP 双桶计数：换 cookie 仍会被 IP 桶挡住；IP 只作辅助，
  它会把 NAT 后的多个人算在一起，所以不单独当身份。
- token 按 token id 计数；额度桶按档位分组：token 的 T0/T1 共用一桶，T2A/T2B/T3
  共用一桶；游客 T0 与 T1 各自一桶（T1 用完后仍可跑 T0）。
- 窗口是自然小时与自然日（不是滑动窗口），`retry_after` 给到窗口结束的秒数。
- 默认额度见 `DEFAULTS`，可被 `web/limits.json` 覆盖；token 记录里的
  `hourly_runs`/`daily_runs`/`max_concurrent` 覆盖该 token 的额度（对它的每个桶生效）。
- 计数落 `web/usage.sqlite3`（gitignore），服务启动时 `cleanup()` 删 30 天前记录。

调用顺序：`check()` → 真正启动 run → `record()`。`check()` 只读库，被拒绝时不消耗额度；
调用方在 429 时不得创建 run 目录。计数库不可用时放行并告警（与审计写失败同口径），
否则磁盘故障会把 T0/T1 一起锁死。
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

import token_admin

WEB = Path(__file__).resolve().parent
DB = WEB / "usage.sqlite3"
LIMITS_FILE = WEB / "limits.json"
RETENTION_DAYS = 30

DEFAULTS = {
    "guest": {
        "t0": {"hourly": 20, "daily": 60, "concurrent": 1},
        "t1": {"hourly": 3, "daily": 10, "concurrent": 1},
    },
    "token": {
        "low": {"hourly": 20, "daily": 50, "concurrent": 1},
        "high": {"hourly": 3, "daily": 8, "concurrent": 2},
    },
    "global": {"concurrent": 4},
}

# token 的档位 → 额度桶后缀；T0/T1 同桶、T2A/T2B/T3 同桶（见设计表的额度行）。
TIER_BUCKETS = {"t0": "low", "t1": "low", "t2a": "high", "t2b": "high", "t3": "high"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    ts REAL NOT NULL,
    bucket TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    ip TEXT NOT NULL,
    action TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_actor ON usage (bucket, actor_id, ts);
CREATE INDEX IF NOT EXISTS usage_ip ON usage (bucket, ip, ts);
"""

_ACTOR_SQL = ("SELECT COUNT(*) FROM usage WHERE bucket = ? AND actor_id = ? "
              "AND action = 'run' AND ts >= ?")
_IP_SQL = ("SELECT COUNT(*) FROM usage WHERE bucket = ? AND ip = ? AND ip <> '' "
           "AND action = 'run' AND ts >= ?")


def bucket_of(actor_kind: str, tier: str) -> str:
    """身份 + 档位 → 额度桶。游客的非 T0 档位在调用前已被 token 门禁挡下，这里取最严的 T1。"""
    if actor_kind != "token":
        return "guest.t0" if tier == "t0" else "guest.t1"
    return "token." + TIER_BUCKETS.get(tier, "high")


def load_limits() -> dict:
    """默认额度叠 `limits.json` 覆盖；文件缺失/损坏回落默认，未知键忽略。"""
    limits = _merge(DEFAULTS, {})
    try:
        raw = json.loads(Path(LIMITS_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return limits
    return _merge(limits, raw if isinstance(raw, dict) else {})


def quota(actor: dict, tier: str) -> dict:
    """某身份在某档位的 `{hourly, daily, concurrent}`；token 记录字段优先于默认。"""
    return _quota(actor, tier, load_limits())


def check(actor: dict, tier: str, active_count: int = 0, *, ip: str = "",
          global_active: int = 0) -> dict:
    """能否再开一次运行。

    `active_count` 是同身份正在跑的 run 数，`global_active` 是全局在跑数（调用方从 ACTIVE
    统计）。返回 `{allowed, retry_after, reason, scope, bucket, limit, used}`：
    `reason` ∈ `hourly`/`daily`/`concurrent`/`storage_error`，`scope` ∈ `actor`/`ip`/`global`。
    `limit`/`used` 只在拒绝时有意义。
    """
    limits = load_limits()
    bucket = bucket_of(str(actor.get("kind") or "guest"), tier)
    actor_id = str(actor.get("id") or "")
    limits_for_actor = _quota(actor, tier, limits)
    global_concurrent = int(limits["global"]["concurrent"])
    if global_active >= global_concurrent:
        return _deny("concurrent", 60, bucket, global_concurrent, global_active, "global")
    if active_count >= limits_for_actor["concurrent"]:
        return _deny("concurrent", 60, bucket, limits_for_actor["concurrent"],
                     active_count, "actor")
    now = time.time()
    try:
        with closing(_connect()) as conn:
            for window, start, end in _windows(now):
                used = _count(conn, _ACTOR_SQL, bucket, actor_id, start)
                if used >= limits_for_actor[window]:
                    return _deny(window, end - now, bucket, limits_for_actor[window],
                                 used, "actor")
                # 只对游客做 IP 桶：token 是明确身份，不能被同一 NAT 的其他 token 连坐。
                if ip and str(actor.get("kind") or "guest") != "token":
                    used_ip = _count(conn, _IP_SQL, bucket, ip, start)
                    if used_ip >= limits_for_actor[window]:
                        return _deny(window, end - now, bucket, limits_for_actor[window],
                                     used_ip, "ip")
    except sqlite3.Error as exc:
        print(f"限流计数不可用，本次放行：{exc}", file=sys.stderr)
        return _allow(bucket, "storage_error")
    return _allow(bucket)


def record(actor: dict, tier: str, *, ip: str = "") -> bool:
    """记一次已启动的运行；写失败只告警，不阻断已经起来的 run。"""
    bucket = bucket_of(str(actor.get("kind") or "guest"), tier)
    try:
        with closing(_connect()) as conn:
            conn.execute("INSERT INTO usage (ts, bucket, actor_id, ip, action) "
                         "VALUES (?, ?, ?, ?, 'run')",
                         (time.time(), bucket, str(actor.get("id") or ""), str(ip or "")))
            conn.commit()
        return True
    except sqlite3.Error as exc:
        print(f"限流计数写入失败：{exc}", file=sys.stderr)
        return False


def cleanup(days: int = RETENTION_DAYS) -> int:
    """删除 `days` 天前的计数记录（服务启动时调用）；返回删除行数。"""
    cutoff = time.time() - max(1, int(days)) * 86400
    try:
        with closing(_connect()) as conn:
            cur = conn.execute("DELETE FROM usage WHERE ts < ?", (cutoff,))
            conn.commit()
            return int(cur.rowcount or 0)
    except sqlite3.Error as exc:
        print(f"限流计数清理失败：{exc}", file=sys.stderr)
        return 0


def _merge(base: dict, override: dict) -> dict:
    """按默认值的键形状合并：只收已知键、只收正整数，其余忽略。"""
    out = {key: dict(value) if isinstance(value, dict) else value
           for key, value in base.items()}
    for key, value in (override or {}).items():
        if key not in out:
            continue
        if isinstance(out[key], dict):
            if isinstance(value, dict):
                out[key] = _merge(out[key], value)
        elif isinstance(value, int) and not isinstance(value, bool) and value > 0:
            out[key] = value
    return out


def _quota(actor: dict, tier: str, limits: dict) -> dict:
    kind, name = bucket_of(str(actor.get("kind") or "guest"), tier).split(".", 1)
    out = dict(limits[kind][name])
    if kind == "token":
        out.update(_token_overrides(str(actor.get("id") or "")))
    return out


def _token_overrides(token_id: str) -> dict:
    """token 记录里的配额字段；缺失或非正整数就留默认。

    配额是服务端策略，不在 `verify()` 的公开字段里，所以直接读 token 库记录。
    """
    if not token_id:
        return {}
    for record in token_admin._read():
        if str(record.get("id") or "") != token_id:
            continue
        out = {}
        for field, key in (("hourly_runs", "hourly"), ("daily_runs", "daily"),
                           ("max_concurrent", "concurrent")):
            value = record.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                out[key] = value
        return out
    return {}


def _windows(now: float) -> list:
    """自然小时与自然日窗口：`(名字, 起点, 终点)`。"""
    hour_start = (int(now) // 3600) * 3600
    local = time.localtime(now)
    day_start = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))
    return [("hourly", hour_start, hour_start + 3600),
            ("daily", day_start, day_start + 86400)]


def _connect() -> sqlite3.Connection:
    path = Path(DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.executescript(_SCHEMA)
    return conn


def _count(conn: sqlite3.Connection, sql: str, bucket: str, key: str, since: float) -> int:
    row = conn.execute(sql, (bucket, key, since)).fetchone()
    return int(row[0]) if row else 0


def _allow(bucket: str, reason: str = "") -> dict:
    return {"allowed": True, "retry_after": 0, "reason": reason, "scope": "",
            "bucket": bucket, "limit": None, "used": None}


def _deny(reason: str, seconds: float, bucket: str, limit: int, used: int,
          scope: str) -> dict:
    return {"allowed": False, "retry_after": max(1, int(math.ceil(seconds))),
            "reason": reason, "scope": scope, "bucket": bucket,
            "limit": int(limit), "used": int(used)}
