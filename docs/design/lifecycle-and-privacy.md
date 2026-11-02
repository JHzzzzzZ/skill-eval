# 生命周期、可信度与多用户隔离设计

状态：**设计；多用户模型暂缓，单用户共享模式保留**。本轮先执行内容保真、进度可信、证据/审计/预检、历史对比与导出；多用户认证/隔离不在本轮。

## 0. 结论先行

当前是**单进程、无身份、全量共享**的本地工具。两个人只要连同一个服务，就会看到同一份 `/api/runs`、报告、日志、工作区和评测集。

**本轮决定：多用户模型先不做。** 保持单用户共享模式，但不再把它包装成“多人隔离”；相关 API 继续按本机单用户设计。多用户认证/隔离留到后续，不阻塞本轮体验修复。

未来若要做，按 `owner_uid`（权限）+ `lineage_id`（同一 skill 的版本组）+ `version_id`（skill_sha256）三层建模；历史查询只查 `owner + lineage`，跨用户默认不共享。同名内容匹配只用于版本归并，不用于授权：完全相同 hash 直接同版本；≥90% 归入同一 lineage；70%–90% 只提示“可能同源”并让人工确认；<70% 新建 lineage。相似度用文件角色加权 + token shingle Jaccard，不用原始字节比例。跨用户同款 skill 默认不可见，显式分享才可见。

## 0.1 本轮执行范围

- P0：内容保真、进度可信、证据抽屉、报告头 provenance、真实审核人、运行前预检。
- P1：评测集前置提示、完整历史、两版对比、报告导出/复制链接。
- 不做：多用户认证、owner/lineage 数据隔离、跨用户分享。数据仍按本机单用户共享，文档与 UI 不再声称隔离。

## 1. 当前泄露面

| 数据 | 现状 | 风险 |
|---|---|---|
| 运行列表 | `/api/runs` 返回全部 run，含绝对 `results`/`report`/`log` 路径 | 谁都能枚举别人的报告和目录结构 |
| 报告 | `/api/report`、`/report` 任意 id 可读；报告含 evidence/data | skill 内容、提示词、输出可能泄露 |
| 日志 | `/api/log` 任意 id 可读 | 运行错误、路径、模型输出 |
| 工作区/评测集 | `/api/workspaces` 返回全部；`/api/evalset/export` 可导出 `evalsets/` 下任意集 | skill 源码、触发集、真实业务输入 |
| 变更 | `/api/stop`、`/api/override`、`/api/evalset/review` 任意 id/路径 | 可停止或篡改别人的评估 |
| 浏览器 | `localStorage.se-last-run` + URL `?run=` | 同一浏览器 profile 共享 |
| 路径参数 | `/api/run` 的 `skill`/`evalset`、`/api/evalset/generate` 的 `out` | 本地文件读取/写入；绑定非 loopback 时风险放大 |

默认 `--host 127.0.0.1` 只对本机开放；同一台机器、同一浏览器、端口转发或 `--host 0.0.0.0` 都会绕过“只有我”的假设。API key 目前不落盘，但 `/api/llm/test` 会消耗服务端配置的额度。

## 1.1 IP 不能做租户键

IP 只能做审计、限流、可选 allowlist，不能做“谁能看哪份报告”的依据：

- 本机两个人都是 `127.0.0.1`，会合并成一个租户。
- 公司/家庭 NAT 多人共享一个公网 IP；DHCP 换网后历史“消失”。
- IPv6 隐私地址会轮换；反向代理的 `X-Forwarded-For` 可伪造（除非只信自己的代理）。
- 同一浏览器即使 IP 不同，`localStorage`/`?run=` 仍共享。

所以 `ip -> data_dir` 只能算“网络位置分组”，不是权限边界。身份必须来自 token / OS profile / 登录会话；IP 只写进审计字段。

## 2. 多用户模型

### 2.0 账号与注册

不要做公开自助注册。对本工具，注册的复杂度远高于收益：

- 公开注册需要邮箱验证、密码重置、限流、滥用/配额、租户清理、合规；还会把“跑任意 skill”变成公开攻击面。
- 推荐 **邀请制 token**：管理员用 CLI 创建 `uid + data_dir + 一次性 token`，用户首次输入 token，服务端存 token hash 并签发 HttpOnly/SameSite=Strict 会话；没有密码库、没有邮件、没有公开入口。
- 如果组织已有 SSO，可接 OIDC；不要自己造密码体系。
- 本机单人模式不需要账号：一人一进程 + 一人一 data-dir 最简单。

结论：账号注册“难”不是技术难点，而是运营与安全成本；先用邀请制，公开注册不做。

### 2.1 评测输入的注入与外泄

被测 skill 是不可信输入。当前 `trace_run` 只把 cwd 指到沙箱副本，**不是安全沙箱**：子进程继承宿主环境，`--guard-repo` 只检测指定仓库的越界写入，不阻止读取宿主文件或网络外发。prompt injection 可以让被测 agent 读取 `~/.pi/agent/auth.json`、环境变量、宿主目录，并把内容写进回答/trace，最后进入报告。

在多用户之前必须先做执行隔离：

1. 用容器/低权限 OS 用户跑 T2/T3：只挂载 `/work` 副本，只读根文件系统，禁止宿主 `$HOME`、Docker socket、SSH/云凭据；环境变量白名单。
2. 默认断网，只允许模型代理/明确 allowlist；模型 key 不放进沙箱，用宿主侧 scoped proxy。
3. 资源限制（CPU/内存/时间/进程数）；trace 采集在沙箱外；`--guard-repo` 继续保留但只作为补充。
4. LLM 评审在沙箱外、无 tools、固定 prompt；把 skill 文本当数据，不执行其中指令；输出 JSON schema/长度校验，证据只作引用，不让 skill 控制报告字段。
5. 报告默认 owner-only；分享前做密钥/敏感信息扫描与显式确认。内容保真不等于自动公开：保留原文，但只给有权限的人看。
6. HTML 全量转义 + CSP；不把模型输出当 HTML/JS。

prompt injection 不能靠“加一句不要听 skill 的”解决；要靠隔离、最小权限、输出校验和人工复核。账号系统只解决“谁能看”，不能解决“沙箱里能读到什么”。

### 方案 A（未来：一人一进程）

一人一进程：

```bash
python web/app.py --data-dir ~/.skill-eval/alice --port 8765
python web/app.py --data-dir ~/.skill-eval/bob   --port 8766
```

- 新增 `--data-dir`：`runs/`、`workspaces/`、`uploads/`、`evalsets/` 全部落在该目录；不再共用包内 `web/runs`。
- 新增 `--token`（或启动时生成一次性 token）：API 必须带 `X-Skill-Eval-Token`；前端首次输入，存 `sessionStorage`，关闭标签页失效。
- 默认拒绝非 `127.0.0.1` 绑定；要绑定局域网必须同时给 token。
- `/api/runs` 只列当前 data-dir 的 run；返回 id、名称、时间、结论，不返回绝对路径。
- 两个人的数据在文件系统层隔离；同一 OS 账号仍可互相读文件，所以“防的是应用层误看，不是防同账号恶意用户”。

代价：每个人要起自己的服务、记自己的端口；跨人对比要显式导出，不会自动出现在同一个最近列表。

### 方案 B（未来：同一个服务多人用）

- `users.json`：token hash → `{uid, name, data_dir}`；登录后发 HttpOnly + SameSite=Strict 会话 cookie。
- 每个 run/workspace/evalset 的 meta 写 `owner_uid`；所有读/写/停/改判/导出都校验 owner。
- `/api/runs` 只返回当前 uid；共享评测集单独标 `shared=true`，只读。
- 分享报告用**显式、限时、只读**的 share token，默认关闭；不把整个 `web/runs` 暴露成静态目录。
- 需要登录页、会话过期、CSRF/Origin 校验、用户管理；工作量明显大于方案 A。
- 注册只做邀请制 token（管理员创建），不做公开自助注册；可选接 OIDC/SSO。

### 方案 C（不推荐）

浏览器 cookie 随机 uid 分目录：没有认证，cookie 可伪造，清 cookie 即丢数据，也无法阻止同机用户读文件。只能算“防串台”，不是安全隔离。

## 3. 内容保真（P0）

问题：`web/index.html::publicText()` 和 `scripts/report.py::_public_text()`/`_public_obj()` 用正则删除所有 `ADR-xxxx`，连 `report.json` 的 `data` 也被清洗。这是数据损坏。

设计：

- 删除运行时 ADR 正则；只保留空白规范化。
- `report.json` 的 `data` 原样保留；不递归清洗用户内容。
- 内部 ADR 编号不进 UI 文案，改为作者约束 + 静态测试；不在运行时删用户文本。
- 回归：static description/error/evidence 写 `ADR-0002`，`report.json`、md、html、日志必须保留；`publicText` 不再改变它。
- 现有“页面不能出现 ADR”的测试改为“UI 源码不引用 ADR；用户内容中的 ADR 保留”。

## 4. 进度可信（P0）

- 去掉固定 `WEIGHT`；按阶段计划 + 当前阶段 case/repeat 数推进，进度只增不减。
- 顶部显示 `已用 12:34 · 预计剩余 20–40 分钟`；范围来自档位估算 + 已完成阶段实测，不装精确。
- 标签页标题：`触发评测 · 12:34`；完成变 `✓ 报告已生成`；可选 Notification，默认不申请权限。
- SSE 已无 60 分钟硬截止，保留 15 秒心跳；断线先查 `/api/status` 再重连。

## 5. 判断可复核（P0）

- 指标行可点开抽屉：摘要、`data` 表格/原始 JSON、检查点证据、优化建议。
- 报告头显示：run id、skill 名/hash、档位、模型、评估器指纹、生成时间、审核人、是否局部报告。
- 人工改判不再写死 `"web"`：输入审核人名（默认本机用户名），后端校验非空，写 `manual-overrides.jsonl`。
- 运行前预检：T2A/T3 无评测集、T2B 无真实输入、未配置模型、非 loopback 无 token，都在 CTA 上方警告；不等到报告里才看到 skipped。

## 6. 生命周期（P1）

- 评测集前置：把“本次使用 v1 / 未创建，将跳过 #10”放在运行 CTA 旁；03 评测集卡不再排在运行之后造成顺序误导。
- 完整历史：`/api/runs?limit=100` + 历史页；当前最近 10 条保留为快捷入口。
- 对比：`GET /api/compare?base=<run>&target=<run>`，逐指标返回 base/target/change；评估器指纹、schema、skill hash 不同则标“不可比”，不硬算回归。
- 导出/分享：report.json、report.md、report.html、zip；分享走显式链接，默认不公开。

## 7. 实施顺序

1. 内容保真（P0，数据损坏）
2. 方案 A 的 data-dir + token + 路径收敛（P0，泄露）
3. 进度/已用/剩余/标题（P0）
4. 证据抽屉 + 报告头 + 审核人 + 预检（P0）
5. 历史对比 + 导出/分享（P1）
6. 方案 B（仅当确认要同一个服务多人登录）

验收底线：用户 A 的 token/data-dir 无法列出、读取、停止、改判、导出用户 B 的 run/workspace/evalset；`report.json` 保留用户原文；进度不倒退；报告头能回答“哪次、用什么标准、谁判的”。
