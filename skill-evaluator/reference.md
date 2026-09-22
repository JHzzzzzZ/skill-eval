# Reference — Skill Evaluator

SKILL.md 的细则层。指标编号 #N 对应 prompt.txt 的 19 条需求。

## 指标 → 测量手段映射

| 手段 | 指标 | 本期状态 |
|---|---|---|
| 静态检查 | #2 name/description 规范与 token 数、#7 调用方式、#13 五组规则扫描（危险命令/凭据/注入/外发/混淆） | ✅ 已实现（static_check.py，ADR-0013） |
| 沙箱运行 | #1 触发精准度、#4 成本、#5 必要性、#8 最小依赖 | ✅ 已实现（trace_run.py + score.py + judges/deps.md 语义面双源） |
| LLM 评审 | #3 正文精简、#6 低冗余、#11 fallback、#16 前置自检、#17/18 输入输出契约、#19 副作用可逆 | ✅ 已实现（judges/ + judge_runner.py） |
| 沙箱运行（多次） | #12 稳定性（组内变异系数）、#15 幂等 | ✅ 已实现（重复运行 ×N + idem.py） |
| 沙箱运行（多模型，可选） | #12 稳定性（跨模型一致率） | ✅ 已实现（model_robust.py，ADR-0011，默认不跑） |
| 触发集自检 | #1 触发精准度的可信度 | ✅ 已实现（evalset_check.py，ADR-0011） |
| Trace 对照 | #10 过程可验证 | ✅ 已实现（process.py） |
| 历史报告 diff | #14 版本演进 | ✅ 已实现（evolution.py） |

## 沙箱

沙箱 = **git worktree**。被测 skill 的副本仓库上 `git worktree add`，运行在 worktree 内进行，评估产物不落回副本主目录。未来若替换为 Docker，只改本节定义，SKILL.md 流程不变。

权限边界（#13）：worktree 方案下只能做到静态扫描 + trace 审计（看 trace 里是否出现了不该有的命令），做不到强制拦截。扫描规则见 `scripts/static_check.py` 内的五组正则（ADR-0013）：危险命令 / 硬编码凭据 / 注入指令 / 数据外发 / 混淆。

已知漏检（如实告知，不要当成"扫过就是安全"）：自造格式的高熵 token、改写的语义注入、跨段拼接的注入、无读取动词的 `.env` 引用。混淆类命中仅警告，需人工复核。

扫描口径（ADR-0012）：默认排除 `tests/`、`evalsets/` 与扫描器自身源码（`--no-default-excludes` 关闭默认排除）；`--exclude <路径>` 的相对路径按 **skill 目录**解析；单行末尾加 `static-check:ignore` 可抑制命中，被抑制项记入 `ignored` 字段（不静默丢）。

## 版本号

1. 被测 skill 自带 `.git` → `git rev-parse --short HEAD`
2. 裸文件夹 → 只读存档原始上传（`uploads/<name>-<时间戳>/`），副本 `git init + commit`，版本号 = `auto-<副本短 hash>`
3. 副本的 `meta.json` 记录 `content_sha256` 兜底；results 目录另有同名 `meta.json`（内容不同，靠 `schema` 字段区分，ADR-0010）

注意区分两个号：本包 SKILL.md frontmatter 的 `version`（给人看的发布号）与报告里的 `auto-<sha256>` 评估器指纹（ADR-0010，用来判定两份报告能不能横向比）。不要互相替代。

相同内容重复上传 → 相同 hash → 结果落同一目录（幂等 #15）。

## 评测集冻结

位置：`evalsets/<skill-name>/v<N>/`，在**评估器目录**，不放被测 skill。

```
evalsets/<name>/v1/
├── triggers/should/*.json       # {"prompt": "..."} — 人工提供 5-10 条，缺则自动生成
├── triggers/should-not/*.json
├── triggers/confusable/*.json
├── cases/*.json                 # {"prompt": "...", "expect": "..."}
└── meta.json                    # {"frozen_at", "reviewed_by": "human"}
```

冻结流程：自动生成 → 展示给用户 → **人工明确确认后**才写 `meta.json` 标记冻结。冻结后所有版本评估复用，不重新生成。`should` 类 prompt 不得照抄 description 措辞（否则 precision 测不出真实值）。

冻结前跑质量自检（ADR-0011）：`python scripts/evalset_check.py <evalset目录> --skill <skill目录>`，查 should prompt 照抄 description、组内近似重复、同句跨组冲突；命中则先改写再冻结，裁决落 `evalset.json`（clean=false → report.py 把 #1 降级为 warn）。

## 运行场景

| 场景 | 做什么 | 服务指标 |
|---|---|---|
| 触发评测 | 三组 prompt 各跑一次，带 `--early-exit`（ADR-0007）：事件流出现首次工具调用即终止该次运行省 token；**triggered 由 `trigger_judge.py` 用 LLM 按最终回答判定**（trace_run.triggered 仅作粗筛），环境故障的 case 不计入 P/R 分母；early-exit 截断的 case 最终回答为空，判定交由 trigger_judge 按已执行的步骤裁决 | #1 |
| 主运行 Golden Run | 干净 worktree + 加载 skill，跑 cases，采 trace | #4/#8/#9 |
| 基线 A/B | 同 cases、同 worktree，但**不加载** skill | #5 |
| 重复运行 ×N | 主运行重复 N 次，每次 trace 存档 | #12（组内）/ #15 |
| 跨模型运行（可选，默认不跑） | 同批 cases 换 `--model` 再跑，trace 落 `models/<模型名>/`，`model_robust.py` 出一致率 | #12（跨模型） |
| 消融运行 | 删掉疑似冗余段落后重跑 | #6 实证（三期） |

## 模型配置

沙箱运行（trace_run.py）的模型三层优先级：

1. 命令行 `--model <provider/id>`（显式，最高）
2. 环境变量 `SKILL_EVAL_MODEL=<provider/id>`（会话级默认，推荐在评估开始前设置）
3. pi 自身的默认模型（兜底）

可选 `--thinking <off|minimal|low|medium|high|xhigh|max>` 控制思考档位。LLM 评审（judges/）用**同一个模型配置**，保证与被测运行同源。

可配参数：重复运行 N=3；IDEMPOTENT_MAX_RATIO=0.5（idem.py）；DESCRIPTION_TOKEN_LIMIT=100、NAME_MAX_CHARS=64、`--exclude <路径>`（相对 skill 目录解析）、`--no-default-excludes`（static_check.py）；F1_PASS=0.7、COST_CV_MAX=0.5（report.py）；ECHO_COVERAGE_MAX=0.6、DUP_COVERAGE_MAX=0.8（evalset_check.py）；ANSWER_SIM_MIN=0.5、AGREEMENT_MIN=0.8（model_robust.py）。

## 执行载体与扩展点（ADR-0007）

沙箱运行的唯一执行载体是 pi CLI 子进程（逐 case 一次 `pi --mode json --no-session`），触发评测带 `--early-exit`（首次工具调用即停，省 token）。**不使用 subagent 机制**。未来若需支持 pi 以外的 agent，在 trace_run.py 之上加 adapter 层；本期只预留此声明，不实现（避免没有第二个实现的抽象）。

被测 skill 出现宿主环境硬编码（绝对路径、特定用户目录、.claude/.cursor 等他方生态路径）→ 可移植性闸门整体 fail（ADR-0008，static_check 的 `hardcoded` 字段）。<!-- static-check:ignore -->

## 规则

- 评估器目录内不得出现针对特定被测 skill 的脚本（#31）；历史运行证据存 `evalsets/<name>/provenance/`
- LLM 评审一律逐项计分：rubric 拆检查点，每项 1 分，score 由 items 推导（#5，judge_runner.py 强制校验）

## 中间产物契约

所有中间文件落盘在 `evalsets/<name>/results/<version>/`（与报告同目录）：

- `triggers.json`：`{"should": [bool...], "should_not": [...], "confusable": [...]}`，布尔值 = 该 prompt 是否实际触发
- `runs.json`：`[{"tool_calls", "tokens", "seconds"}, ...]`，golden（有 skill）各次运行
- `baseline.json`：同 runs.json 格式，基线（无 skill）运行，缺省则 necessity 输出 `skipped`
- `trace-<序号>.json`：`{"steps": [{"tool", "args", "args_hash"}, ...], "answer", "tokens", "seconds", "triggered", "errors"}`（trace_run.py 落盘，--out）
- `static.json`：static_check.py stdout（#13 的五组：`dangerous`/`secrets`/`injection`/`exfil`/`obfuscation`，另有 `excluded`/`ignored`/`hardcoded`；契约见脚本 docstring）
- `score.json`：score.py stdout
- `idem.json`：多次 idem.py 结果的聚合 `{"ratio", "idempotent"}`
- `golden.json`：主运行的 golden trace（#8 唯一数据源）
- `compare.json`：compare.py 裁决（#9）
- `process.json`：process.py 裁决（#10）
- `ablation.json`：`{"f1_full", "f1_ablated", "deleted": [...]}`（#6 实证）
- `evalset.json`：evalset_check.py stdout（触发集质量，#1 降级依据）
- `model_robust.json`：model_robust.py stdout（#12 跨模型一致率）
- `models/<模型名>/trace-<序号>.json`：跨模型运行的逐 case trace（序号 = 评测集顺序，各模型必须一致）
- `meta.json`：`{"schema": "skill-eval/results-meta/1", "generated_at", "conclusion", "evaluator", "skill_fingerprint", "evalset_source", "metrics"}`（report.py 写入，ADR-0010）；已存在时**合并保留**人工填写的键（model/sandbox/skipped_reason 等），不静默覆盖
- `judges/<metric>.json`：judge_runner.py 校验通过的 LLM 评审输出，metric ∈ {brevity, redundancy, fallback, precheck, contract, side-effects}（**不要**存成 judge-*.json 或放 results 根目录，report.py 只认 judges/<metric>.json）

`score.py <results目录>` 读同目录三件套；`idem.py <trace1> <trace2>` 出幂等比例。统计口径：均值/标准差/n/min/max（#10/#28）；必要性为 tokens/tool_calls/seconds 三分量各自提升率（#11）。

## LLM 评审

temperature=0、单次、结构化 JSON 输出（`{items: [{name, pass, quote}], score, evidence, reason}`，evidence 必须引用原文；**score = pass 项数/总项数**，0~1 两位小数，judge_runner.py 强制校验）。rubric 文件在 `judges/`。

流程门禁：主 agent 按 rubric 产出 JSON → 运行 `python scripts/judge_runner.py --validate <文件>` → `valid=true` 才能进报告；校验失败把 errors 原文回给 LLM 重产一次，再失败则该指标标 `skipped` 并注明。

后续可切换为 3 次取中位数或多模型交叉——切换时只改本节，rubric 不动。

## 幂等（#15）

重复运行的 trace 两两跑 `idem.py`：第二次重复第一次已完成步骤的比例 > 0.5 → 不幂等，进报告 `#15` 项。

## 稳定性与跨模型（#12）

#12 有两个独立信号，报告里分写不合并（ADR-0011）：

- **组内**：重复运行 ×N 的 tool_calls 变异系数 ≤ COST_CV_MAX(0.5) → 稳定；n < 2 时只记"无稳定性证据"
- **跨模型**：`model_robust.py` 的一致率 ≥ AGREEMENT_MIN(0.8) → 鲁棒；默认不跑（成本 ×模型数）

裁决口径保守：跨模型只能**降级**（发现漂移 → warn），不能升级——它不能代替组内重复运行。
case 一致 = triggered 全同 且 回答相似（字符 3-gram Jaccard ≥ 0.5；全空回答视为无信息，early-exit 截断不算不一致）。

## 报告契约

```
evalsets/<name>/results/<version>/
├── report.json   # 19 个 key=指标名，值={verdict: pass|warn|fail|skipped, method, data, note}
│                 # 顶层另带 evaluator（评估器指纹）与 generated_at，不占指标 key
├── report.md     # 每条指标一节：分数/证据/建议（顶部显示评估器指纹）
└── meta.json     # {"schema": "skill-eval/results-meta/1", "generated_at", "conclusion",
                  #  "evaluator", "skill_fingerprint", "evalset_source", "metrics"}（report.py 写入；
                  #  已存在时合并保留人工字段：model/sandbox/skipped_reason 等，ADR-0010）
```

规则：
- 19 条指标全部出现；未测的标 `skipped` 并注明原因，不打 0 分
- 不合成加权总分；结论分三档：必测全过 / 有警告 / 有失败
- `report.json` 机器可读，供 #14 版本演进做横向 diff
- `report.py <results目录> --skill <被测skill目录> [--evalset <evalsets/<name>/vN>]`：`--skill` 让 meta.json 记下被测 skill 的内容指纹——vN 目录命名下唯一能看出"评的是哪份内容"的来源
- 评估器指纹不一致的版本之间，指标差异不构成回归证据（evolution.py 输出 `comparable=false`）

## Fallback

| 故障 | 处置 |
|---|---|
| 被测路径无 SKILL.md | 终止，报告"不是合法 skill 包" |
| scripts/judges 缺失 | 报告评估器自身损坏，列出缺失项，终止 |
| 硬依赖缺失（`bash scripts/check-deps.sh` 报 FAIL） | 终止：缺 python≥3.9/git 时会静默降级，先装齐再跑 |
| 缺 pi CLI 或模型（自检仅 warn） | 只能跑静态检查；沙箱类与 LLM 评审指标标 `skipped`，报告注明原因 |
| worktree 创建失败 | 换临时目录 + 提示隔离降级，继续跑 |
| 沙箱运行崩溃 | 降级为"静态 + LLM 评审"，报告标注哪些指标因降级 `skipped` |
| 评测集未审核 | #1/#5/#9 标 `skipped`，其余照常 |
| 触发集未做质量自检 | #1 只按 F1 判（不降级），meta.json 里评测集来源照记 |
| 跨模型未跑 | #12 只出组内信号，不扣分 |

## 三期工具

- `scripts/process.py --skill <SKILL.md> --trace <trace.json>`：#10 过程审计，LLM 对照声明与 trace 找无意义步骤，裁决 `{meaningless, score, reason}` 落 `process.json`
- `scripts/ablation.py --skill <dir> --delete "标题" --out <dir>`：#6 消融——按 ## 标题删段生成消融副本，用消融副本重跑触发评测对比 F1（judges/redundancy.md 的 evidence 提供候选段落）
- `scripts/evolution.py evalsets/<name>`：#14 版本演进——读 results/*/report.json，输出跨版本每指标 verdict 变化与最新结论

三者实调 LLM 时同样走 `SKILL_EVAL_MODEL` 环境变量或显式 `--model`。

## 可信度工具（ADR-0010 / ADR-0011）

- `scripts/evalset_check.py <evalset目录> --skill <skill目录>`：#1 可信度——3-gram 覆盖率查照抄/重复/冲突，裁决落 `evalset.json`（`clean=false` → report.py 把 #1 降级为 warn）
- `scripts/model_robust.py <results目录>`：#12 跨模型一致率——读 `models/<模型名>/trace-<序号>.json`，裁决落 `model_robust.json`；单模型或无 `models/` 目录 → `skipped`
