# CONTEXT

被评估对象是 skill，不是服务。评估器本身也是一个 skill。

## 词汇表

### Skill
待评估的 skill 包：`SKILL.md`（frontmatter + 正文）及可选的 `reference.md`、脚本等附属文件。

### 指标（Metric）
16 条评估维度之一（如精准触发、低成本性）。编号先按档位 T0→T3、再按旧编号连续排列（ADR-0032）；每条指标有**一个主测量手段**，可另有辅证来源：
- **静态检查**：脚本确定性检查，无 LLM
- **LLM 评审**：带 rubric 的模型打分，输出 JSON（分数 + 原文引用 + 理由）
- **沙箱运行**：实际跑 skill，采集 trace 后计算

主源优先，辅证原形状保留在报告 `data` 里，不做加权合成（ADR-0004/0017）。多源指标：#2（静态行数主源 + 语义评审）、#12（trace 主源 + 语义评审 + 静态声明面）、#4（静态六组主源 + PII 语义评审）、#5（评审；消融实证随 T4 休眠）、#8/#9（共用一份 contract 评审）。
_Avoid_: 一条指标只能有一个数据来源

### 稳定性（#15）
同一 case 内多次运行的离散度，以**逐 case 变异系数（std/mean of tool_calls）中位数**裁决（ADR-0016）。pooled 变异系数只作参考——它含 case 间的难度差，会把「任务不同」读成「跑不稳」。
_Avoid_: 用 pooled cv 当稳定性结论

### 运行场景（Scenario）
一次沙箱执行。多个指标共用同一次运行，分 6 类：触发评测（T2A）、真实场景 A/B（T2B，同一真实输入的 golden + baseline，可多条）、主运行（T3 Golden Run）、基线运行（T2B A/B）、重复运行（T3）、跨模型运行（T3，可选，默认不跑）。消融运行随 T4 休眠。

### 档位（Tier）
评估深度的增量等级：T0 静态 / T1 LLM 评审 / T2A 触发评测 / T2B 真实场景 A/B / T3 核心沙箱（ADR-0033）。高档复用低档产物；低档先行、后续补齐称**增量补跑（staged）**；沙箱故障退回低档才称**降级（fallback）**。T4 暂不开放。
_Avoid_: 模式、级别

### 执行载体（Harness）
T2+ 实际运行 skill 的 agent 载体：当前是 pi CLI，后续可加 Code Agent。T1 的 LLM 评审不走执行载体，走 LLM 接口层（ADR-0034）。
_Avoid_: 把 LLM 接口层也叫 Harness

### LLM 接口层（LLM Interface）
T1 评审与触发判定直接调用的模型接口（OpenAI-compatible 优先），不经过 agent 框架，固定无工具、单发（ADR-0034）。

### 编排器（Runner）
把档位固化为确定性阶段的入口（T0→T3→report），负责串流程、落中间产物、发进度事件；agent 与前端共用同一套契约（ADR-0036）。
_Avoid_: 把 runner 当成第二个评估器（它不重算指标）

### 前端（Web UI）
本地页面入口：选工作区/档位/执行载体/模型/评测集/真实输入，SSE 看进度，不重算指标；只绑 `127.0.0.1`（ADR-0036/0039）。

### 真实输入（Real Input）
用户在 T2B 提供的 1–20 条真实业务输入，用于逐条跑 golden 与 baseline 两臂；只有 `prompt` 必填，`note` 仅显示，`expect` 是验收标准。与冻结评测集分开，存本地 `uploads/`，报告只回显哈希 + 备注（ADR-0040）。

### 评测集（EvalSet）
固定不变的测试数据，按 skill 名 + 版本持久化。包含触发集（应触发/不应触发/易混淆）和结果集（输入 prompt + 期望输出）。冻结前经 LLM 自动审核（换模型）；**一旦冻结不再重新生成**，版本演进只换被测 skill。

### 审核人（Reviewer）
评测集冻结前的把关方，二值：`ai`（LLM 自动审核，与生成阶段不同源）或 `human`（人工逐条复核，可由 ai 升级覆写，不回滚）。报告对 ai 口径显式标注。
_Avoid_: 审核模型（那是执行审核的模型配置，不是把关方身份）

### 触发集质量
触发集本身是否可信：should prompt 是否照抄 description、组内是否近似重复、同句是否跨组冲突。照抄会让触发评测测的是字面匹配而非语义触发，precision/recall 虚高。命中 → #10 降级为 warn（不是整体闸门）。

### 可移植性闸门
static_check 的编号检查项：被测 skill 出现宿主环境硬编码（绝对路径、特定用户目录、.claude/.cursor 等他方生态路径）→ 整体 fail。只查硬编码，不查"声明给某 agent 用"（那是 #3 调用方式的事）。命中在报告里落到 **#4 裁决 = fail**（ADR-0015/0030）——闸门不属于任何一条独立指标，但必须有一个能承载 fail 的指标，否则 `conclusion()` 看不见它。

### Trace
一次沙箱运行的完整记录：工具调用序列、每步 token、耗时、产物路径、报错。

### Report
评估产出。`report.json`（机器可读，16 个指标 key，附 `schema: 2`、`legacy_id_map`、档位与评估器指纹字段）+ `report.md`（人可读）+ `report.html`（--html：report.md 的渲染媒介 + 评测集审核界面）+ `meta.json`（本次运行的元数据：评估器指纹、被测 skill 指纹、评测集来源）。report.json/md/html 同一份 data，**网页不得比 Markdown 少信息**。未测的指标标 `skipped`，不打 0 分。

### 报告模式（Schema）
`report.json` 的契约版本。schema 2 = 16 条指标 + 新编号；跨 schema 不构成回归证据（ADR-0032）；evolution 已随 T4 休眠。

### 优化建议（Advice）
报告里可执行的待办项，每项带 `#N` 反链（`{metric, severity, problem, evidence, action}`）。由 `report.py` 从裁决**确定性派生**，不经 LLM——建议与问题同源，结构上不可能脱钩。落地三处：每个指标卡下方内联、report.html 的「优化建议」tab 汇总、report.md 的 `## 优化建议` 段（同一份数据，ADR-0030）。
_Avoid_: 让 LLM 生成建议（会与指标脱钩）

### 依赖全面性（Dependency Completeness）
"脚本实际用到的工具"是否都在文档里声明过（`deps_used` − `deps_declared` = `deps_missing`）。与**依赖新鲜度**区分：后者查反方向（声明了但包内不存在，`stale_refs`）。缺失声明只降 #12 为 warn，不 fail（ADR-0031）。

### 评估器指纹（Evaluator Fingerprint）
评估器自身内容（scripts + judges + `config.yaml` + SKILL.md + reference.md）的 sha256，形如 `auto-3f9c1a7b`。用来回答"这份报告是哪个评估器产生的"：指纹不同的版本之间，指标差异不构成被测 skill 的回归证据。与「版本」区分：版本指被测 skill 的迭代，指纹指评估器的迭代。

### 版本（Version）
同一 skill 的不同迭代。版本对比 = 读历史 report.json 做横向 diff，不重跑旧版本。
