# CONTEXT

被评估对象是 skill，不是服务。评估器本身也是一个 skill。

## 词汇表

### Skill
待评估的 skill 包：`SKILL.md`（frontmatter + 正文）及可选的 `reference.md`、脚本等附属文件。

### 指标（Metric）
19 条评估维度之一（如精准触发、低成本性）。每条指标归属恰好一种测量手段：
- **静态检查**：脚本确定性检查，无 LLM
- **LLM 评审**：带 rubric 的模型打分，输出 JSON（分数 + 原文引用 + 理由）
- **沙箱运行**：实际跑 skill，采集 trace 后计算

### 运行场景（Scenario）
一次沙箱执行。多个指标共用同一次运行，共 5 类：触发评测、主运行（Golden Run）、基线运行（A/B）、重复运行、消融运行。

### 档位（Tier）
评估深度的五个增量等级：static / review / trigger / core / full（T0–T4）。高档复用低档产物；低档先行、后续补齐称**增量补跑（staged）**；沙箱故障退回低档才称**降级（fallback）**。
_Avoid_: 模式、级别

### 评测集（EvalSet）
固定不变的测试数据，按 skill 名 + 版本持久化。包含触发集（应触发/不应触发/易混淆）和结果集（输入 prompt + 期望输出）。冻结前经 LLM 自动审核（换模型）；**一旦冻结不再重新生成**，版本演进只换被测 skill。

### 审核人（Reviewer）
评测集冻结前的把关方，二值：`ai`（LLM 自动审核，与生成阶段不同源）或 `human`（人工逐条复核，可由 ai 升级覆写，不回滚）。报告对 ai 口径显式标注。
_Avoid_: 审核模型（那是执行审核的模型配置，不是把关方身份）

### 可移植性闸门
static_check 的编号检查项：被测 skill 出现宿主环境硬编码（绝对路径、特定用户目录、.claude/.cursor 等他方生态路径）→ 整体 fail。只查硬编码，不查"声明给某 agent 用"（那是 #7 调用方式的事）。

### Trace
一次沙箱运行的完整记录：工具调用序列、每步 token、耗时、产物路径、报错。

### Report
评估产出。`report.json`（机器可读，key 为指标名，附档位字段）+ `report.md`（人可读）。未测的指标标 `skipped`，不打 0 分。

### 版本（Version）
同一 skill 的不同迭代。版本对比 = 读历史 report.json 做横向 diff，不重跑旧版本。
