# Reference — Skill Evaluator

SKILL.md 的细则层。指标编号 #N 对应 prompt.txt 的 19 条需求。

## 指标 → 测量手段映射

| 手段 | 指标 | 本期状态 |
|---|---|---|
| 静态检查 | #2 name/description 规范与 token 数、#7 调用方式、#13 五组规则扫描（危险命令/凭据/注入/外发/混淆）、#3 正文行数（主源） | ✅ 已实现（static_check.py，ADR-0013/0017） |
| 沙箱运行 | #1 触发精准度、#4 成本、#5 必要性、#8 最小依赖 | ✅ 已实现（trace_run.py + score.py + judges/deps.md 语义面双源） |
| LLM 评审 | #3 正文精简（语义两项，辅证）、#6 低冗余、#11 fallback、#16 前置自检、#17/18 输入输出契约、#19 副作用可逆 | ✅ 已实现（judges/ + judge_runner.py） |
| 沙箱运行（多次） | #12 稳定性（组内变异系数）、#15 幂等 | ✅ 已实现（重复运行 ×N + idem.py；#12 口径见 ADR-0016） |
| 沙箱运行（多模型，可选） | #12 稳定性（跨模型一致率） | ✅ 已实现（model_robust.py，ADR-0011，默认不跑） |
| 触发集自检 | #1 触发精准度的可信度 | ✅ 已实现（evalset_check.py，ADR-0011） |
| Trace 对照 | #10 过程可验证 | ✅ 已实现（process.py） |
| 历史报告 diff | #14 版本演进 | ✅ 已实现（evolution.py） |

多源指标（ADR-0017）：一条指标可有**一个主源 + N 个辅证**，裁决主源优先，辅证以原始形状保留在 `data` 里不丢、不加权合成：#3（静态行数主源 + 语义评审辅证）、#8（trace 报错主源 + `judges/deps.json`）、#6（评审 + 消融实证，消融覆盖）、#17/#18（共用一份 contract 评审）。

## 沙箱

沙箱 = **git worktree**。被测 skill 的副本仓库上 `git worktree add`，运行在 worktree 内进行，评估产物不落回副本主目录。未来若替换为 Docker，只改本节定义，SKILL.md 流程不变。

每次 run 前重置工作树（ADR-0018）：触发/沙箱 run 共用同一个 worktree 时，前一个 run 写下的文件会被后一个 run 读到（实测并发探针里，一条 run 写的 `scratch/scan_all.py` 被另一条 run 当成仓库内容分析）。跑之前 `git -C <worktree> clean -fdx`，或每个 run 单独 `git worktree add`。

prompt 不写沙箱外路径（ADR-0018）：worktree 只限定 cwd，不限定 agent 的写权限——实测一条带真实仓库路径的探针 prompt 让 agent 走出 worktree，改了评估器仓库自己的 `README.md`（已回滚）。评测集里的 prompt 只能引用沙箱内路径。

权限边界（#13）：worktree 方案下只能做到静态扫描 + trace 审计（看 trace 里是否出现了不该有的命令），做不到强制拦截。扫描规则见 `scripts/static_check.py` 内的五组正则（ADR-0013）：危险命令 / 硬编码凭据 / 注入指令 / 数据外发 / 混淆。

已知漏检（如实告知，不要当成"扫过就是安全"）：自造格式的高熵 token、改写的语义注入、跨段拼接的注入、无读取动词的 `.env` 引用。混淆类命中仅警告，需人工复核。

扫描口径（ADR-0012）：默认排除 `tests/`、`evalsets/` 与扫描器自身源码（`--no-default-excludes` 关闭默认排除）；`--exclude <路径>` 的相对路径按 **skill 目录**解析；单行末尾加 `static-check:ignore` 可抑制命中，被抑制项记入 `ignored` 字段（不静默丢）。

## 版本号

1. 被测 skill 自带 `.git` → `git rev-parse --short HEAD`
2. 裸文件夹 → 只读存档原始上传（`uploads/<name>-<时间戳>/`），副本 `git init + commit`，版本号 = `auto-<副本短 hash>`
3. 副本的 `meta.json` 记录 `content_sha256` 兜底；results 目录另有同名 `meta.json`（内容不同，靠 `schema` 字段区分，ADR-0010）

换行保真（ADR-0019）：副本、副本内的 worktree 必须与存档**字节一致**，否则同一份上传会产出两个
`content_sha256`，ADR-0003/0010 的指纹就认不出“评的是哪份内容”（实测 Windows `core.autocrlf=true`
时 51 个文件里 15 个被重写成 CRLF）。副本 repo 一律先关转换再开始干活：

```bash
mkdir <副本> && tar -cf - -C <存档> . | tar -xf - -C <副本>   # 字节拷贝，别用 git clone
cd <副本> && git init -q && git config core.autocrlf false && git add -A && git commit -qm snapshot
git worktree add <沙箱目录> HEAD                                # worktree 继承副本的本地 config
```

校验：对存档与 worktree 逐个文件比字节，必须 0 处不同（实测修复前 51 个里 15 个不同）。

注意区分两个号：本包 SKILL.md frontmatter 的 `version`（给人看的发布号）与报告里的 `auto-<sha256>` 评估器指纹（ADR-0010，用来判定两份报告能不能横向比）。不要互相替代。

相同内容重复上传 → 相同 hash → 结果落同一目录（幂等 #15）。

## 评测集冻结

位置：`evalsets/<skill-name>/v<N>/`，在**评估器目录**，不放被测 skill。

```
evalsets/<name>/v1/
├── triggers/should/*.json       # {"prompt": "..."} — 每组至少 10 条（可配，见 evalset_count.py），人工提供不足则自动生成补齐
├── triggers/should-not/*.json
├── triggers/confusable/*.json
├── cases/*.json                 # {"prompt": "...", "expect": "..."}
└── meta.json                    # {"frozen_at", "reviewed_by": "human"|"ai"}
```

冻结流程：自动生成 → **条数闸门**（`evalset_count.py <evalsets/<name>/vN>`：三组触发集各 ≥ 最小条数，默认 10，可经 `--min` / `SKILL_EVAL_TRIGGER_MIN` / `--min-should|--min-not|--min-confusable` 配置，不达标不进入审核）→ **触发集质量自检**（`evalset_check.py <evalset目录> --skill <skill目录>`：照抄/重复/冲突先改写）→ **LLM 自动审核**（见 § 评测集 AI 审核）→ 通过即冻结：写 `meta.json`，`reviewed_by: "ai"`，**不阻塞后续档位**。人工可后续逐条复核，全部通过后把 `reviewed_by` 覆写为 `"human"`（升级，不回滚）。冻结后所有版本评估复用，不重新生成。`should` 类 prompt 不得照抄 description 措辞（否则 precision 测不出真实值）。

审核载体是 `report.py --html` 产出的 report.html「评测集审核」tab：顶部展示被测 skill 的 name/description（`--skill <被测skill目录>` 指定，缺省兖底读 `uploads/<name>-<时间戳>/SKILL.md` 存档），让审核人先知道在审什么。逐条标通过/驳回后点「提交 review.json」直接落盘（showSaveFilePicker，存到 `reviews/`，浏览器记住上次目录；不支持的浏览器回退为下载），复制/下载保留作兜底。AI 审核与人审共用同一 review.json 落盘格式（来源字段区分）。

## 评测集 AI 审核

审核人与评测集**生成阶段不得用同一个模型**，避免同源自评。模型来源优先级：

1. CLI `--review-model <provider/id>`（显式，最高）
2. 环境变量 `SKILL_EVAL_REVIEW_MODEL=<provider/id>`
3. 都未设置时：**首次运行交互选择**——列出 `pi --list-models` 的可用模型（排除当前默认模型）让用户挑一次，选定后记住供本次会话使用

审核内容：逐条标通过/驳回（与人工审核同一标准），通过率达标才冻结；不通过把驳回原因回给生成阶段修复后重审。报告口径：`report.py` 读 meta.json 的 `reviewed_by`，为 `"ai"` 时注明「评测集 AI 审核，未经人工复核」。

冻结前跑质量自检（ADR-0011）：`python scripts/evalset_check.py <evalset目录> --skill <skill目录>`，查 should prompt 照抄 description、组内近似重复、同句跨组冲突；命中则先改写再冻结，裁决落 `evalset.json`（clean=false → report.py 把 #1 降级为 warn）。

## 运行场景

档位（tier）定义增量递进的评估深度（ADR-0014），入口参数 `--tier <static|review|trigger|core|full>`（或 T0–T4），**默认 T2**。高档复用低档产物：结果目录里各中间产物记录生成时的 skill content_sha256，一致则复用，不一致则作废重跑。低档先行、后续增量补跑（staged）不重做已完成档。

| 档 | 场景 | 做什么 | 服务指标 |
|---|---|---|---|
| T0 | 静态检查 | static_check.py | #2 #7 #13 #3（行数主源） |
| T1 | LLM 评审 | 逐项按 judges/ rubric 评审（4 并发） | #3（语义两项，辅证） #6(评审面) #11 #16 #17 #18 #19 |
| T2 | 触发评测 | 三组 prompt 各跑一次，带 `--early-exit`（ADR-0007 修订、ADR-0018）：事件流出现**首次指向被测 SKILL.md 的工具调用**（渐进式披露下 = agent 决定加载 skill）即终止该次运行省 token——判定按**路径解析**（相对路径按同串 `cd` 目标/运行 cwd 解析），不是绝对路径子串；未出现该调用的 run 跑完整，其行为正是 precision 的证据；**triggered 由 `trigger_judge.py` 用 LLM 判定**，判定口径与 early-exit 同步：**加载即触发，不要求任务实际完成**，截断运行（回答为空）按已执行步骤裁决（trace_run.triggered 仅作粗筛），环境故障的 case 不计入 P/R 分母。**`#7 resolved=human` 时整档不跑**：#1 记 `skipped`（载体测不到“按 description 触发”，ADR-0018） | #1 |
| T3 | 主运行 Golden Run | 干净 worktree + 加载 skill，跑 cases，采 trace | #4/#8/#9 |
| T3 | 基线 A/B | 同 cases、同 worktree，但**不加载** skill | #5 |
| T3 | 重复运行 ×N | 主运行重复 N 次，每次 trace 存档 | #12（组内）/#15 |
| T3 | 跨模型运行（可选，默认不跑） | 同批 cases 换 `--model` 再跑，trace 落 `models/<模型名>/`，model_robust.py 出一致率 | #12（跨模型） |
| T4 | 消融运行 | 删掉疑似冗余段落后重跑 | #6 实证（三期） |
| T4 | 过程审计 / 版本演进 | process.py / evolution.py | #10/#14 |

## 模型配置

沙箱运行（trace_run.py）的模型三层优先级：

1. 命令行 `--model <provider/id>`（显式，最高）
2. 环境变量 `SKILL_EVAL_MODEL=<provider/id>`（会话级默认，推荐在评估开始前设置）
3. pi 自身的默认模型（兜底）

可选 `--thinking <off|minimal|low|medium|high|xhigh|max>` 控制思考档位。LLM 评审（judges/）与触发判定（trigger_judge.py）用**同一个模型配置**，保证与被测运行同源。评测集 AI 审核是唯一例外，见 § 评测集 AI 审核。

可配参数：重复运行 N=3；IDEMPOTENT_MAX_RATIO=0.5（idem.py）；DESCRIPTION_TOKEN_LIMIT=100、NAME_MAX_CHARS=64、SKILL_MD_BODY_MAX_LINES=150（static_check.py；#3 行数主源，超限仅 warn，ADR-0017）、`--exclude <路径>`（相对 skill 目录解析）、`--no-default-excludes`（static_check.py）；触发集每组最小条数 10（evalset_count.py，环境变量 SKILL_EVAL_TRIGGER_MIN / CLI --min*）；F1_PASS=0.7、COST_CV_MAX=0.5（report.py，#12 作用于逐 case 变异系数中位数，无逐 case 数据时回落 pooled，见 ADR-0016）；ECHO_COVERAGE_MAX=0.6、DUP_COVERAGE_MAX=0.8（evalset_check.py）；ANSWER_SIM_MIN=0.5、AGREEMENT_MIN=0.8（model_robust.py）；TRIGGER_PASS_SCORE=0.5（trigger_judge.py，--threshold 可覆盖）；JUDGE_SAMPLES=1（LLM 评审单发次数，3 = 逐项取中位数，ADR-0020）。

## 执行载体与扩展点（ADR-0007）

沙箱运行的唯一执行载体是 pi CLI 子进程（逐 case 一次 `pi --mode json --no-session`），触发评测带 `--early-exit`（首次指向被测 SKILL.md 的调用即停，省 token；ADR-0007 修订）。**不使用 subagent 机制**。未来若需支持 pi 以外的 agent，在 trace_run.py 之上加 adapter 层；本期只预留此声明，不实现（避免没有第二个实现的抽象）。

被测 skill 出现宿主环境硬编码（绝对路径、特定用户目录、.claude/.cursor 等他方生态路径）→ 可移植性闸门整体 fail（ADR-0008，static_check 的 `hardcoded` 字段）。<!-- static-check:ignore -->

## 规则

- 评估器目录内不得出现针对特定被测 skill 的脚本（#31）；历史运行证据存 `evalsets/<name>/provenance/`
- LLM 评审一律逐项计分：rubric 拆检查点，每项 1 分，score 由 items 推导（#5，judge_runner.py 强制校验）

## 中间产物契约

所有中间文件落盘在 `evalsets/<name>/results/<version>/`（与报告同目录）：

- `triggers.json`：`{"should": [bool...], "should_not": [...], "confusable": [...]}`，布尔值 = 该 prompt 是否实际触发
- `runs.json`：`[{"case", "tool_calls", "tokens", "seconds"}, ...]`，golden（有 skill）各次运行；`case` 可选，**全条目都带**时 score.py 产出 `cost_by_case`（#12 组内口径的数据源，ADR-0016）；缺一条即不产出
- `baseline.json`：同 runs.json 格式，基线（无 skill）运行，缺省则 necessity 输出 `skipped`
- `trace-<序号>.json`：`{"steps": [{"tool", "args", "args_hash"}, ...], "answer", "tokens", "seconds", "triggered", "errors", "host": {"model", "pi", "platform"}}`（trace_run.py 落盘，--out；host 为宿主 pin，事后归因/复现用；离线 --events 重算时仅记 model 意图）
- `static.json`：static_check.py --out（脚本自写 UTF-8；stdout 同步回显。禁 shell 重定向——GBK 控制台会产 GBK 文件）。全部脚本入口接 `scripts/_console.py::fix()`：GBK 控制台下回显不可编码字符降级为替换符，不崩、退出码 0；`--out` 文件不受影响。含 #13 的五组（`dangerous`/`secrets`/`injection`/`exfil`/`obfuscation`）、`hardcoded`、`excluded`/`ignored`（ADR-0012）、`scan_excluded_dirs`（ADR-0012：运行面之外）、`stale_refs`（依赖新鲜度，ADR-0009 延伸：SKILL.md 声明但包内不存在的路径引用，warning 级）、`skill_md_body_lines`/`skill_md_body_max_lines`（#3 行数主源与自描述阈值，ADR-0017：口径 = frontmatter 之后全文行数，含空行与代码块）
- `score.json`：score.py --out（同上）；含 `cost`（多次运行汇总）+ `cost_by_case`（逐 case，仅全条目带 case 时出现）
- `idem.json`：多次 idem.py --out 结果的聚合 `{"ratio", "idempotent"}`
- `golden.json`：主运行的 golden trace（#8 唯一数据源）
- `compare.json`：compare.py 裁决（#9）
- `process.json`：process.py 裁决（#10）
- `ablation.json`：`{"f1_full", "f1_ablated", "deleted": [...]}`（#6 实证）
- `evalset.json`：evalset_check.py stdout（触发集质量，#1 降级依据）
- `model_robust.json`：model_robust.py stdout（#12 跨模型一致率）
- `models/<模型名>/trace-<序号>.json`：跨模型运行的逐 case trace（序号 = 评测集顺序，各模型必须一致）
- `meta.json`：`{"schema": "skill-eval/results-meta/1", "generated_at", "conclusion", "evaluator", "skill_fingerprint", "evalset_source", "metrics"}`（report.py 写入，ADR-0010）；已存在时**合并保留**人工填写的键（model/sandbox/skipped_reason 等），不静默覆盖
- `judges/<metric>.json`：judge_runner.py 校验通过的 LLM 评审输出，metric ∈ {brevity, redundancy, fallback, precheck, contract, side-effects}（**不要**存成 judge-*.json 或放 results 根目录，report.py 只认 judges/<metric>.json）
- `raw/judge-events/<metric>.events.jsonl`：该次评审的 pi 事件流原文（ADR-0020，可选但推荐——pi 无温度参数，可复现性靠证据留档）

`score.py <results目录>` 读同目录三件套；`idem.py <trace1> <trace2>` 出幂等比例。统计口径：均值/标准差/n/min/max（#10/#28）；必要性为 tokens/tool_calls/seconds 三分量各自提升率（#11）。

## LLM 评审

**调用口径（ADR-0020/0022）**：每条 rubric 一次**独立单发**调用——prompt = rubric 全文 + 被测文件（+ rubric 自声明的附属文件），不携带编排 agent 的历史/探索上下文，且必须带 **`--no-tools`**（实测带工具时判官会 cd 进真实仓库、自造实验、在包目录里留下宿主硬编码文件）；固定可重放。pi CLI 没有采样参数（无 `--temperature`/`--seed`），所以“temperature=0”写不了，只能靠“同一 prompt + 单发 + 无工具 + 留原始事件流”压住漂移：实测同 prompt 重复 4 次逐项一致，而同一 rubric 换带上下文的 agent 判会得出不同分（#16 1.0 vs 0.5）。默认单次；要更高的可信度就开 `JUDGE_SAMPLES=3` 取中位数（成本 ×3，需在报告注明口径）。

输出：结构化 JSON（`{items: [{name, pass, quote}], score, evidence, reason}`，evidence 必须引用原文；**score = pass 项数/总项数**，0~1 两位小数，judge_runner.py 强制校验）。rubric 文件在 `judges/`。

流程门禁：主 agent 按 rubric 产出 JSON → 运行 `python scripts/judge_runner.py --validate <文件>` → `valid=true` 才能进报告；校验失败把 errors 原文回给 LLM 重产一次，再失败则该指标标 `skipped` 并注明。原始事件流存 `raw/judge-events/<metric>.events.jsonl`（裁决有争议时可回看模型到底看到了什么）；事件流里出现 `tool_execution_start` 则该次评审作废重跑（ADR-0022）。

检查点极性：每个 rubric 的检查点分两向——**能力清单**（做到才 pass，如 precheck 的环境校验置前）与**问题清单**（无此类问题才 pass，如 deps 的无未打包外部依赖）。极性逐条定义，同一 rubric 内可混向（实际也混）；items 契约与 score 公式不感知极性，新写 rubric 时只需保证每条检查点的 pass 条件在文案里可判定，不需要把措辞统一成一向。

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
├── report.json   # tier + 19 个 key=指标名，值={verdict: pass|warn|fail|skipped, method, data, note}
│                 # 顶层另带 evaluator（评估器指纹）、reviewed_by 与 generated_at，不占指标 key
├── report.md     # 每条指标一节：分数/证据/建议（顶部显示档位/审核口径/评估器指纹）
├── report.html   # --html：report.md 的渲染媒介 + 评测集审核界面；报告 tab 不得比 report.md 少信息
└── meta.json     # {"schema": "skill-eval/results-meta/1", "generated_at", "conclusion",
                  #  "evaluator", "skill_fingerprint", "evalset_source", "metrics", tier}（report.py 写入；
                  #  已存在时合并保留人工字段：model/sandbox/skipped_reason 等，ADR-0010）
```

规则：
- `tier` 记录本次跑到的最高档位；#14 evolution diff 时先比 tier 再比分数，档位不同不直接比指标值
- 各中间产物记录生成时的 skill content_sha256；高档复用低档产物时校验一致，不一致则该产物作废重跑
- 19 条指标全部出现；未测的标 `skipped` 并注明原因，不打 0 分
- **data 渲染**（report.md / report.html 共用一套）：已知形状（#15 `per_pair`、#12 `cost_by_case`+`cv_by_case`、#4 `cost`、#9 `per_case`）→ 摘要行 + 表格；HTML 侧表格之后一律附折叠的原始 JSON（`report.json` 同源），judge 面（`items`）另加 score/reason 摘要行；无表可渲染时 ≤400 字符内联、>400 折叠（md 侧 >400 只写「完整 data 见 report.json」指针）；单元格 >800 字符截断并标注。**不得静默截断**（旧实现 `[:2000]` 会把 2165 字符的 #15 data 切成非法 JSON）。渲染层不得反向改 `report.json` 的形状
- 评测集 `reviewed_by: "ai"` 时，报告与 #1/#5/#9 的结论处注明「评测集 AI 审核，未经人工复核」
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
| 沙箱运行崩溃 | 降级（fallback）到最近一个已完成的低档：报告标注缺失指标与原因，不静默丢指标 |
| 审核模型不可用 | 评测集保持未冻结状态，提示人工审核路径，不阻塞已完成的低档位报告 |
| 评测集未审核 | #1/#5/#9 标 `skipped`，其余照常 |
| 触发集未做质量自检 | #1 只按 F1 判（不降级），meta.json 里评测集来源照记 |
| 跨模型未跑 | #12 只出组内信号，不扣分 |
| `disable-model-invocation: true`（#7=human） | #1 标 `skipped`：不跑触发评测（ADR-0018）；已实测的数据留在 `data` 里作证据 |

## 三期工具

- `scripts/process.py --skill <SKILL.md> --trace <trace.json>`：#10 过程审计，LLM 对照声明与 trace 找无意义步骤，裁决 `{meaningless, score, reason}` 落 `process.json`
- `scripts/ablation.py --skill <dir> --delete "标题" --out <dir>`：#6 消融——按 ## 标题删段生成消融副本，用消融副本重跑触发评测对比 F1（judges/redundancy.md 的 evidence 提供候选段落）
- `scripts/evolution.py evalsets/<name>`：#14 版本演进——读 results/*/report.json，输出跨版本每指标 verdict 变化与最新结论

三者实调 LLM 时同样走 `SKILL_EVAL_MODEL` 环境变量或显式 `--model`。

## 可信度工具（ADR-0010 / ADR-0011 / ADR-0016）

- `scripts/evalset_count.py <evalsets/<name>/vN>`：冻结前置的**条数闸门**——三组触发集各 ≥ 最小条数（默认 10，可配），并提示 `meta.json counts` 与实际的差异；不达标不进入审核
- `scripts/evalset_check.py <evalset目录> --skill <skill目录>`：#1 可信度——3-gram 覆盖率查照抄/重复/冲突，裁决落 `evalset.json`（`clean=false` → report.py 把 #1 降级为 warn）
- `scripts/model_robust.py <results目录>`：#12 跨模型一致率——读 `models/<模型名>/trace-<序号>.json`，裁决落 `model_robust.json`；单模型或无 `models/` 目录 → `skipped`
