# 分层访问与审计设计

状态：**已实现并验收（2026-11-02）**。T2/T3 需要持久 token；T0/T1 游客可用；申请表单由人维护，不在本工具内做注册。

## 0. 访问矩阵

| 角色 | T0/T1 | T2/T3 | 历史/报告/日志 | 评测集 |
|---|---|---|---|---|
| 游客（无 token） | 可运行 | 禁止 | 仅当前浏览器游客会话创建的 run | 不可用 |
| 持 token 用户 | 可运行 | 可运行 | 仅自己 token 创建的 run | 可生成/审核/导入导出 |
| 管理员 | 不登录网页 | 不登录网页 | 只通过文件/CLI 管理 token | 只通过 CLI 发 token |

T2/T3 的 token 是**访问控制**，不是执行沙箱。当前 `trace_run` 仍继承宿主环境、只限定 cwd，T2/T3 的不可信 skill 仍可能读宿主文件/外联；上 token 前必须知道这一点，沙箱隔离另做。

## 1. Token

- 持久 token：服务端存 hash，重启不失效；客户端 `localStorage` 保存明文，请求带 `X-Skill-Eval-Token`。
- 格式 `se_<32 bytes urlsafe>`；服务端只存 `sha256(token)`、`id`、`name`、`created_at`、`active`、`note`。
- 存储 `web/tokens.json`（gitignore）；无文件 = 没有可用 token。
- 管理员 CLI（不建网页注册，路径用全局参数指定，不读环境变量）：
  ```bash
  python web/token_admin.py issue --name "张三" [--note "申请表 #12"]
  python web/token_admin.py list
  python web/token_admin.py revoke --id tok_ab12cd34
  # 换库/换审计文件：--tokens-file / --audit-file 写在子命令前
  python web/token_admin.py --tokens-file /tmp/tokens.json --audit-file /tmp/audit.jsonl list
  ```
  `issue` 只打印一次明文 token；申请表单由人维护，审批后管理员执行 issue。
- `GET /api/token/check`：验证 header，返回 `{ok,name,id}`；不返回 hash。
- 空 token/未知/停用一律 401；错误信息不区分“格式错”和“不存在”，避免枚举。

## 2. 游客身份

- 首次请求发 `se_guest` cookie：随机 UUID、HttpOnly、SameSite=Strict、Path=/、30 天；仅用于游客历史归属，不是安全身份。
- 游客 T0/T1 run 的 `run.json` 写 `owner_kind=guest`、`owner_id=<guest_id>`、`protected=false`。
- 游客 `/api/runs` 只返回同 guest_id 的未保护 run；token 用户不返回游客 run，反之亦然。
- 游客不能访问评测集接口（status/items/generate/review/import/export），UI 隐藏/禁用评测集卡。
- 游客 T1 仍消耗 LLM；后续加每 IP 限流（本轮先记录，不静默放开）。

## 3. Run 归属

`web/runs/<id>/run.json` 增加：

```json
{"owner_kind":"guest|token|legacy","owner_id":"...","owner_name":"...","protected":true,
 "tier":"t2a","skill_name":"...","skill_sha256":"...","created_at":"..."}
```

- T2/T3：`protected=true`，必须有效 token 且 `owner_id` 匹配。
- T0/T1：游客或 token 都可创建；按创建者归属。
- 旧 run 没有 owner：标 `legacy`；**只有 token 库为空**（单用户迁移期）才放行，一旦发过任意 token，legacy run 既不列出也不可读——否则任何人都能读到全部历史。
- `/api/run` 的 `reuse_from` 同样校验归属：只复制自己可访问的 run，否则 403 + 审计 `reuse_denied`，且不建新 run 目录。
- 所有按 run id 的接口先校验归属：`/api/status`、`/api/report`、`/report`、`/api/log`、`/api/export`、`/api/compare`、`/api/stop`、`/api/override`。
- `/api/compare` 两份都必须属于当前 actor；跨用户对比只能走显式导出，不直接读对方 run。

## 4. 审计

- 追加写 `web/audit.jsonl`（gitignore），一行一个事件；不记录 token 明文、API key、prompt/回答正文。
- 字段：`ts`、`ip`、`x_forwarded_for`（原样，标记不可信）、`user_agent`、`actor_kind`、`actor_id`、`actor_name`、`action`、`method`、`path`、`status`、`run_id`、`tier`、`skill_sha256`、`detail`。
- IP 取 `handler.client_address[0]`；不把 `X-Forwarded-For` 用于授权。
- 必记事件：token 校验成功/失败、游客 run、T2/T3 拒绝、run start/stop、report/log/export 读取、compare、override、评测集 review/import/export、token 管理 CLI。
- 审计写入失败不阻断主流程，但要写 stderr；文件权限尽量 0600。

## 5. UI

- 顶栏/高级区增加“访问 token”输入 + 验证按钮；状态显示“游客”/“已授权：姓名”。
- token 存 `localStorage`，刷新后自动 `/api/token/check`；失效则清空。
- 选 T2/T3 且无有效 token：`开始评估` 禁用或点击提示“T2/T3 需要申请 token”；T0/T1 保持游客可用。
- 游客隐藏评测集卡和 T2/T3 选项？T2/T3 可显示但标注需要 token；评测集卡显示“需要 token”而不是暴露路径。
- 历史列表按后端过滤；token 用户只看到自己的 run。
- 申请表单不实现；页面只给一句“T2/T3 需要申请 token”，具体入口由部署方提供。

## 6. 验收

- 无 token 跑 T0/T1 成功；T2/T3 返回 401。
- 有效 token 跑 T2/T3 成功；另一 token 读该 run 返回 403/404。
- 游客 A/B 互相看不到对方 run；token A/B 互相看不到。
- 旧 run 在 token 库为空时可读；发过 token 后 legacy run 返回 403 且不出现在 `/api/runs`。
- `audit.jsonl` 有 IP/UA/actor/action/status，且没有 token/API key。
- 公开注册页不存在；`token_admin.py issue` 能发持久 token，revoke 后立即失效。
