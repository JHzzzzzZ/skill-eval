# 可用性下一批：预估、进度、报告、下一步、审核、单用户

状态：**Phase A–D 已实现并验收**。本批不新增第三方依赖、不新增环境变量；运行时仍是 stdlib。

优先级：先做“跑之前知道成本”和“报告能当文档”，再做“下一步/审核”，最后做单用户模式。

## 实现状态（2026-11-02）

| 方案项 | 状态 | 说明 |
|---|---|---|
| §1 成本预估 | 已实现（Phase A） | `estimate.py` + `/api/estimate`；按实际 case/真实输入显示 calls/agent/tokens/minutes；缓存阶段裁剪 |
| §2 动态进度 | 已实现（Phase A） | estimate weights 驱动 stageBounds；case 进度继续按 index/total；超时文案改为“比预估慢” |
| §2 配置记忆 | 已实现（Phase A） | `localStorage["se-form"]` 保存档位/重跑/真实输入/模型设置/工作区/评测集；不存 API key |
| §3 证据表格 | 已实现（`cd99d0c`） | 证据弹层表格化，原始 JSON 折叠 |
| §3 溯源评测集 | 已实现（`cd99d0c`） | 显示评测集名称/版本 |
| §3 导出证据 | 已实现（`cd99d0c`） | 带 judges/triggers/traces 与 score 等 |
| §3 改判历史 | 已实现（`cd99d0c`） | 独立弹层展示原值/新值/理由/审核人/时间 |
| §3 指标锚点/筛选/焦点锁/手机报告卡片 | 已实现（Phase B） | `#m-N` 深链定位/高亮；筛选只切可见行；七个弹窗统一 Esc+焦点锁；≤640px 表格转卡片 |
| §4 建议汇总/优先级/工作量 | 已实现（Phase B） | `report.advice` 增加确定性 `priority`/`effort`；结果区「下一步」卡 + 全部建议弹层 |
| §4 单指标重跑 | 已实现（Phase D） | `rerun_metrics.py` + `/api/rerun-metrics` + 报告行“重跑”；spike 测试验证其他 rubric 字节不变 |
| §5 审核质量面板/搜索/过滤/未审状态 | 已实现（Phase C） | 质量面板展示条数闸门 + 自检文件/覆盖率；默认未审、搜索/组/状态过滤、可见行全选与批量；review.json 增量合并 |
| §6 单用户模式 | 已实现（`cc7494a`） | `--single-user`；guest/local/legacy 历史放行，只拒 token |

注意：Phase A–D 已实现；单指标重跑有独立 spike 测试锁“只改目标 rubric、其他文件字节不变”。`cd99d0c` 是上一轮 P1 修复，不等同于本方案 §3 的全部内容。

## 评审补充（2026-11-02）

这轮评审提出的 P0/P1/P2 已同步进方案，不与六角度方案重复计算：

| 项 | 处置 | 当前状态 |
|---|---|---|
| P0 单用户历史 | `--single-user` 放行 guest/local/legacy，只拒 token；旧 guest 报告仍可见可打开 | 已实现 `cc7494a` |
| P0 403 文案 | 按状态码分支：403=身份不匹配，404=不存在；附导出/导入替代路径 | 已实现 `cd99d0c` |
| P1 证据表格 | 与报告页一致的检查点/结果/原文引用表格，原始 JSON 折叠 | 已实现 `cd99d0c` |
| P1 溯源评测集 | 溯源条显示评测集名称 + 版本 | 已实现 `cd99d0c` |
| P1 导出证据 | zip 带 judges/triggers/traces + score 等白名单 | 已实现 `cd99d0c` |
| P1 改判历史 | 独立视图：指标、原值、新值、理由、审核人、时间 | 已实现 `cd99d0c` |
| P1 手机首屏 | topbar 换行；窄屏 token 移入弹层 | 已实现 `cd99d0c` |
| P2 单指标重跑 | 先做 spike，证明只发一个 rubric 且不清空其他裁决；通过再实现 | 未实现，门槛见下 |

六角度方案中仍未实现的部分以“实现状态”表为准；不得把 `cd99d0c` 的 P1 修复当成六角度方案整体完成。

## 1. 跑之前：本次成本预估

### 数据来源

新增 `scripts/estimate.py`（纯函数，runner 与 Web 共用）：

输入：`tier`、评测集目录、真实输入条数、`repeat`、缓存命中阶段、历史 trace。
输出：

```json
{
  "calls": {"min": 7, "max": 37},
  "agent_runs": 30,
  "tokens": {"min": 120000, "max": 420000},
  "minutes": {"min": 20, "max": 90},
  "stages": [{"name":"T1","calls":7,"agent_runs":0,"weight":18}],
  "cached": ["T0","T1"],
  "assumptions": ["T2A 按 30 条触发 case、0–30 次兜底判定估算"]
}
```

计算口径：

| 阶段 | 模型调用 | agent 子进程 |
|---|---|---|
| T1 | 7 个 rubric（缓存命中则 0） | 0 |
| T2A | 0–触发 case 数（早停成功则 0，未早停才调 `trigger_judge`） | 触发 case 数 |
| T2B | 真实输入数 ×（有 expect 时 1 次 compare，否则 2 次 pairwise） | 真实输入数 × 2 |
| T3 | 有 expect 的 case 数 × 1 次 compare | case 数 × repeat |

- token：优先读该 skill 历史 `traces/*.json`、`t2b/cases/*/with.json` 的中位数 / p90；没有历史时用 `config.yaml` 的 `estimate.*` 默认值。
- 时间：同样优先历史 `seconds`；LLM 调用按可配的默认延迟；给区间，不给单点。
- 缓存：调用 `runner.stage_ready()` 只算未完成阶段；全部缓存时显示“预计几秒”。
- 钱：默认只显示调用次数与 token 区间；只有配置了模型单价才显示金额，避免把未知价格说成确定值。
- 实测校准：每次 run 结束把实际 calls/tokens/seconds 写进 `run-meta.json`；下次同 skill 用实际中位数替换默认值。

### API 与 UI

- `GET /api/estimate?skill=&tier=&evalset=&real_cases=&real_expects=&repeat=`；不启动 run，不扣配额。
- 页面在档位/评测集/真实输入变化后防抖刷新，显示：
  - `本次约 7–37 次模型调用`
  - `约 30 个 agent 子进程`
  - `约 20–90 分钟`
  - `约 120k–420k tokens`
  - `已缓存：基础检查、文档评审（预计减少 7 次调用）`
- 切换档位时立刻更新；不把写死的 `TIER_ESTIMATE_MIN` 当唯一来源。

## 2. 进度与配置记忆

### 动态权重

- `runner` 的 `plan` 事件增加 `weights`（或 Web 用 `/api/estimate` 的 `stages[].weight`）。
- 权重按实际工作量估算：T2A 按触发 case 数，T2B 按真实输入 ×2，T3 按 case ×repeat；T1 按 7 个 rubric。
- 阶段内继续用 `case.index/total` 填进度；T3 不再固定停在 80%。
- `setProgress` 保持只增不减，但允许“阶段内预计完成时间”单独下降/上升，不把预估写成事实。

### 超时文案

从“已超过预估，仍在运行”改为：

> 这次比预估慢，可能原因：模型排队、长 case、容器启动或重试；任务仍在运行。

同时显示实际已用时间 + 当前阶段，不显示“卡住”。

### 配置记忆

`localStorage["se-form"]` 保存（不含 API key）：

```json
{"tier":"t3","harness":"claude-code","backend":"llm","protocol":"openai",
 "model":"...","base_url":"...","workspace":"...","evalVersion":"...",
 "realCases":[...],"rerun":{...},"reviewer":"..."}
```

- 页面加载/工作区恢复后回填；用户改配置即保存。
- API key 不持久化；只保留本次页面内存。
- 增加“恢复上次配置”提示，不覆盖用户刚手动改的值。

## 3. 报告当文档

- 指标行加 `id="m-2"`；复制链接支持 `?run=<id>#m-2`，打开后滚动并高亮。
- 指标表加筛选：`全部 / 只看失败 / 只看警告 / 只看未测`；筛选只改可见行，不重算指标。
- 弹窗统一：
  - `Esc` 关闭；
  - 打开时记录焦点，`Tab/Shift+Tab` 锁在弹窗内；
  - 关闭后恢复原焦点；
  - `role="dialog"` + `aria-modal="true"`。
- 手机：`@media(max-width:640px)` 把指标表变卡片：
  - 隐藏表头；
  - 每行 `display:block`，单元格用 `data-label` 显示字段名；
  - 移除 `min-width:760px` 对报告卡片的约束。
- 保留桌面表格，不做两套数据源。

## 4. “然后呢”：建议与单指标重跑

### 建议清单

- `report.advice` 已有严重度；新增确定性字段：
  - `priority`：fail=1 / warn=2 / skipped=3；
  - `effort`：`quick`（文档/命名/契约类）、`medium`（评测集/触发/依赖）、`large`（T3 成本/稳定性）。
- 不作为 16 指标之外的评分，只影响展示排序。
- Web 结果区顶部加“下一步”卡：
  - 失败/警告计数；
  - 前 3 条建议（问题 → 动作，带 `#N` 反链）；
  - “查看全部建议”打开汇总弹层。
- 现有每行证据里的建议保留，不重复造数据。

### 单指标重跑

- T1 类指标（#2/#4 语义/#5–#9）：`runner --rerun-metrics "#2,#5"` 只重跑对应 `judge_run.py`，保留其他 rubric，预计 1–2 次调用。
- T2A #10 → 只重跑 T2A；T2B #11/#12 → 只重跑 T2B；T3 #13–#16 → 只重跑 T3。按钮上直接标“预计 N 次调用”，用户知道不是免费的。
- 单指标重跑复用同一 run 目录、同一归属/限流/审计；失败不影响已完成的其他指标。
- 第一阶段只做 T1 单 rubric；T2/T3 仍按阶段重跑，避免把 case 级恢复复杂度引进报告层。

## 5. 评测集审核

- 加载评测集时同时调现有 `GET /api/evalset/status`，在审核表上方展示：
  - 条数闸门是否通过；
  - `evalset_check` 的照抄 description / 组内重复 / 跨组冲突 / 越界路径；
  - 每条问题的文件名与覆盖率，可点击定位。
- 审核表：
  - 默认“未审”，不再默认全“通过”；
  - 搜索 prompt；
  - 按组过滤：should / should-not / confusable / case；
  - 按状态过滤：未审 / 通过 / 驳回；
  - 全选仅作用于当前可见行；
  - 顶部显示“已审 12/30”；提交前仍要求审核人。
- `GET /api/evalset/items` 附带 `review`（来自 `reviews/review.json`），刷新后恢复已审状态。
- 批量通过需要二次确认，并提示质量自检结果；质量自检不替代人工判断。

## 6. 本机单用户模式

新增显式启动开关，不做网页自助开关：

```bash
python web/app.py --single-user
```

- 仅在绑定 `127.0.0.1` 时允许；绑定非 loopback 直接拒绝启动，避免把无鉴权服务暴露出去。
- 单用户模式：
  - T2/T3 不再要求 token；评测集接口不再要求 token；
  - 隐藏 token 输入/评测集 token 门禁；
  - run 归属写 `owner_kind="local"`；
  - **历史归属：`_owner_matches` 放行 guest/local/legacy，只拒绝 token run**——旧浏览器 cookie 失效后，旧 guest 报告仍在列表里且能打开；token run 来自多人模式，单用户模式不冒充其归属；
  - 仍保留 IP/UA/action 审计；
  - 导出/对比仍校验 run id 合法性。
- 多人模式保持现有 token/guest 行为；模式写进 `run.json`/meta，切换后旧 run 按 `legacy` 规则可见，不迁移、不改归属。
- 不做“自动检测单人”，避免默认削弱鉴权；用户显式选择。

## P2 开工门槛：单指标重跑必须先做 spike

当前缓存是**阶段级**：T1 的 `runner` 重跑会清空 `judges/` 再整档跑，不能直接拿来承诺“只发一个 rubric 的调用”。Phase 3 开工前先做一次性 spike：

1. 准备一个已有 7 个 rubric 的 run，记录 7 个 `judges/*.json` 的字节 hash 与 `report.json` 的 16 条裁决。
2. 只对 `#2` 调用 `judge_run.py --metric brevity`，写入临时 overlay，不碰其他 6 个文件。
3. 用 7 份合并后的 judge 重新跑 `report.py`，断言：
   - 只发出 1 次模型调用（fake LLM 计数）；
   - 其他 6 个 rubric 文件字节不变；
   - 16 条指标仍在，其他 15 条裁决不变；
   - T1 阶段缓存写入后仍是完整 7 份，不会把半截当完成。
4. 通过才实现 `--rerun-metrics`；不通过就取消该 UI 入口，保留阶段级重跑，避免给用户假的“单指标”收益。

## 实施顺序与验收

1. **Phase 1（1–2 天）**：`estimate.py` + `/api/estimate` + UI 成本条；动态进度权重；配置记忆；超时文案。
2. **Phase 2（1–2 天）**：指标锚点/筛选/弹窗 Esc+焦点锁/移动卡片；建议汇总卡。
3. **Phase 3（2–3 天）**：审核质量面板 + 未审/搜索/过滤/批量；先过上面的单 rubric spike，再决定是否开放 T1 单指标重跑。
4. **Phase 4（0.5 天）**：`--single-user` 与 loopback 限制。

验收：

- 20 条真实输入 + 30 条触发集时，页面显示按实际数量计算的 calls/agent/tokens/时间区间，不是固定 2–45 分钟。
- T3 进度不停在固定 80%；超时文案不再暗示卡死。
- 刷新/重开页面恢复档位、真实输入、模型设置（不含 key）。
- `?run=...#m-2` 可定位；筛选、Esc、Tab、手机卡片均可用。
- 建议汇总按 fail→warn→info 排序；T1 单 rubric 重跑只发对应调用。
- 审核页显示质量自检，默认未审，已审状态刷新不丢。
- `--single-user` 只监听 loopback，T2/T3 无 token，历史跨浏览器可见；默认模式不变。
