# 分层访问与审计实现方案

状态：**已执行并验收（2026-11-02）**。设计见 `docs/design/access-and-audit.md`。不做注册页/申请表；只做 token 存储、CLI 发 token、T2/T3 门禁、游客 T0/T1、审计。

## 文件

- 新增 `skill-evaluator/web/token_admin.py`
- `skill-evaluator/web/app.py`
- `skill-evaluator/web/index.html`
- `skill-evaluator/tests/test_web.py`
- 新增 `skill-evaluator/tests/test_token_admin.py`
- `.gitignore`
- `skill-evaluator/reference.md`
- `skill-evaluator/AGENTS.md` / `tests/AGENTS.md` 计数

## 1. Token 存储与 CLI

`web/token_admin.py`：

- `TOKENS_FILE = web/tokens.json`；结构 `{"tokens":[{id,name,hash,created_at,active,note}]}`。
- `issue(name, note="")`：生成 `se_` + `secrets.token_urlsafe(32)`，存 `sha256`，打印一次明文；返回 record（不含 hash）。
- `list_tokens()`：不打印 hash。
- `revoke(id)`：`active=false`。
- CLI：`issue --name --note`、`list`、`revoke --id`。
- 写文件用临时文件 + replace；POSIX `chmod 0600`；Windows 不强制。
- `.gitignore` 加 `skill-evaluator/web/tokens.json`、`skill-evaluator/web/audit.jsonl`。

## 2. 后端门禁

`web/app.py`：

- 常量：`TOKENS_FILE`、`AUDIT_FILE`、`GUEST_COOKIE="se_guest"`。
- `_load_tokens()` / `_verify_token(handler)`：读 `X-Skill-Eval-Token`，sha256 后 constant-time 比较 active 记录；缓存到 `handler._token`。
- `_ensure_guest(handler)`：解析 Cookie；无则生成 UUID，标记 `handler._new_guest`，后续 `_send_json`/`_send_bytes`/HTML/report 响应加 `Set-Cookie: se_guest=...; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000`。
- `_actor(handler)`：token 记录或 `{kind:"guest", id:guest_id}`。
- `_run_access(handler, rid)`：
  - 读 `run.json`；`owner_kind=="token"` 必须 token id 匹配；
  - `guest` 必须 guest id 匹配；
  - `legacy`（旧 run 无 owner）迁移期允许（文档标注）；
  - 没有 run.json 返回 False。
- `_audit(handler, action, status, run_id="", tier="", detail="")`：追加 `web/audit.jsonl`；字段按设计；IP 取 `client_address[0]`，`X-Forwarded-For` 原样记录但不授权；不写 token/key/正文。
- `GET /api/token/check`：返回 `{ok,name,id}` 或 401；审计成功/失败。
- `/api/run`：
  - tier t2a/t2b/t3：无有效 token → 401，审计 `run_denied`；有 token 继续。
  - t0/t1：游客或 token 都可；有 token 用 token owner，否则 guest owner。
  - `run.json` 写 `owner_kind/owner_id/owner_name/protected`；T2/T3 protected=true。
- `/api/runs`、`/api/status`、`/api/report`、`/report`、`/api/log`、`/api/export`、`/api/compare`、`/api/stop`、`/api/override`、`/api/events`：先 `_run_access`，失败 403（不存在仍 404 的语义按现有实现尽量保持）；`/api/compare` 两份都校验。
- `/api/evalset/status|items|generate|review|import|export`：要求有效 token，否则 401；游客 UI 隐藏评测集卡。
- 审计：token check、run start/denied、stop、report/log/export/compare、override、eval review/import/export。
- 旧 run 的 `legacy` 迁移：无 owner 时允许访问；新 run 全部写 owner。

## 3. 前端

- 顶栏/高级区加 token 输入：`#accessToken`、`#tokenBtn`、`#tokenState`；`localStorage["se-token"]`；启动时 `/api/token/check`。
- `authHeaders()` 返回 `X-Skill-Eval-Token`；所有需要鉴权的 fetch 带它（run/runs/status/report/log/export/compare/stop/override/eval 接口）。
- T2/T3 无 token：`run` 点击提示“T2/T3 需要申请 token”，不启动；T0/T1 继续游客。
- 游客隐藏评测集卡；token 用户显示。
- 导出报告改为 `fetch("/api/export?id=")` + blob 下载（`<a href>` 不能带 header）；复制链接不带 token。
- 历史/报告仍按后端过滤；游客只看到自己 cookie 的 run，token 用户只看自己的 run。
- 显示 token 状态：“游客”/“已授权：姓名”；失效清 localStorage。

## 4. 测试

- `test_token_admin.py`：issue 只返回一次明文；list 不含 hash；revoke 后 verify 失败；hash 不落明文。
- `test_web.py`：
  - 无 token T0 成功、T2A 401；
  - 有 token T2A 成功；
  - token A 不能读 token B 的 run（403/404）；
  - guest A/B 互相不可见；
  - `/api/evalset/*` 无 token 401；
  - audit 有 ip/ua/actor/action/status 且无 token/key；
  - UI 有 token 输入/T2/T3 门禁/评测集隐藏。
- 全量 + static + py_compile + JS 语法。

## 验收

```bash
cd skill-evaluator
python -m pytest tests -q
python scripts/static_check.py .
python -m py_compile web/app.py web/token_admin.py scripts/runner.py scripts/report.py
```

冒烟：issue token → 游客跑 T0 → T2A 401 → 带 token 跑 T2A → 另一 token 读不到 → 查看 audit.jsonl 的 IP/UA/actor；游客 T1 仍可用。
