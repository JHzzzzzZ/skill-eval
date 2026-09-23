# CONTEXT

被评估对象是 skill，不是服务。评估器本身也是一个 skill。

## 词汇表

### Skill
待评估的 skill 包：`SKILL.md`（frontmatter + 正文）及可选的 `reference.md`、脚本等附属文件。

### 指标（Metric）
19 条评估维度之一（如精准触发、低成本性）。每条指标有**一个主测量手段**，可另有辅证来源：
- **静态检查**：脚本确定性检查，无 LLM
- **LLM 评审**：带 rubric 的模型打分，输出 JSON（分数 + 原文引用 + 理由）
- **沙箱运行**：实际跑 skill，采集 trace 后计算

主源优先，辅证原形状保留在报告 `data` 里，不做加权合成（ADR-0004/0017）。多源指标：#3（静态行数主源 + 语义评审）、#8（trace 主源 + 语义评审）、#6（评审 + 消融实证）、#17/#18（共用一份 contract 评审）。
_Avoid_: 一条指标只能有一个数据来源

### 稳定性（#12）
同一 case 内多次运行的离散度，以**逐 case 变异系数（std/mean of tool_calls）中位数**裁决（ADR-0016）。pooled 变异系数只作参考——它含 case 间的难度差，会把「任务不同」读成「跑不稳」。
_Avoid_: 用 pooled cv 当稳定性结论

### 运行场景（Scenario）
一次沙箱执行。多个指标共用同一次运行，共 6 类：触发评测、主运行（Golden Run）、基线运行（A/B）、重复运行、跨模型运行（可选，默认不跑）、消融运行。

### 档位（Tier）
评估深度的五个增量等级：static / review / trigger / core / full（T0–T4）。高档复用低档产物；低档先行、后续补齐称**增量补跑（staged）**；沙箱故障退回低档才称**降级（fallback）**。
_Avoid_: 模式、级别

### 评测集（EvalSet）
固定不变的测试数据，按 skill 名 + 版本持久化。包含触发集（应触发/不应触发/易混淆）和结果集（输入 prompt + 期望输出）。冻结前经 LLM 自动审核（换模型）；**一旦冻结不再重新生成**，版本演进只换被测 skill。

### 审核人（Reviewer）
评测集冻结前的把关方，二值：`ai`（LLM 自动审核，与生成阶段不同源）或 `human`（人工逐条复核，可由 ai 升级覆写，不回滚）。报告对 ai 口径显式标注。
_Avoid_: 审核模型（那是执行审核的模型配置，不是把关方身份）

### 触发集质量
触发集本身是否可信：should prompt 是否照抄 description、组内是否近似重复、同句是否跨组冲突。照抄会让触发评测测的是字面匹配而非语义触发，precision/recall 虚高。命中 → #1 降级为 warn（不是整体闸门）。

### 可移植性闸门
static_check 的编号检查项：被测 skill 出现宿主环境硬编码（绝对路径、特定用户目录、.claude/.cursor 等他方生态路径）→ 整体 fail。只查硬编码，不查"声明给某 agent 用"（那是 #7 调用方式的事）。

### Trace
一次沙箱运行的完整记录：工具调用序列、每步 token、耗时、产物路径、报错。

### Report
评估产出。`report.json`（机器可读，key 为指标名，附档位与评估器指纹字段）+ `report.md`（人可读）+ `report.html`（--html：report.md 的渲染媒介 + 评测集审核界面）+ `meta.json`（本次运行的元数据：评估器指纹、被测 skill 指纹、评测集来源）。report.json/md/html 同一份 data，**网页不得比 Markdown 少信息**。未测的指标标 `skipped`，不打 0 分。

### 评估器指纹（Evaluator Fingerprint）
评估器自身内容（scripts + judges + SKILL.md + reference.md）的 sha256，形如 `auto-3f9c1a7b`。用来回答"这份报告是哪个评估器产生的"：指纹不同的版本之间，指标差异不构成被测 skill 的回归证据。与「版本」区分：版本指被测 skill 的迭代，指纹指评估器的迭代。

### 版本（Version）
同一 skill 的不同迭代。版本对比 = 读历史 report.json 做横向 diff，不重跑旧版本。
