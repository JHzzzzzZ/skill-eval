# Reference — Skill Evaluator

SKILL.md 的细则层。16 条指标编号见 ADR-0032（按档位连续）。脚本共用 `_console` / `_subprocess` / `_config` 三个内部模块。

## 指标 → 测量手段映射

| 手段 | 指标 | 本期状态 |
|---|---|---|
| 静态检查 | #1 name/description 规范与字符预算、#3 调用方式、#4 六组规则扫描（危险命令/凭据/注入/外发/混淆/个人信息）、#2 正文行数（主源，150 warn / 500 fail）、#12 依赖全面性 | ✅ 已实现（static_check.py，ADR-0013/0017/0029/0031/0037） |
| 沙箱运行 | #10 触发精准度、#13 成本、#11 必要性、#12 最小依赖 | ✅ 已实现（trace_run.py + score.py + judges/deps.md 语义面双源） |
| LLM 评审 | #2 正文精简（语义两项，辅证）、#5 低冗余、#6 fallback、#7 前置自检、#8/#9 输入输出契约、#4 PII 语义面 | ✅ 已实现（judges/ + judge_runner.py） |
| 沙箱运行（多次） | #15 稳定性（组内变异系数）、#16 幂等 | ✅ 已实现（重复运行 ×N + idem.py；#15 口径见 ADR-0016） |
| 沙箱运行（多模型，可选） | #15 稳定性（跨模型一致率） | ✅ 已实现（model_robust.py，ADR-0011，默认不跑） |
| 触发集自检 | #10 触发精准度的可信度 | ✅ 已实现（evalset_check.py，ADR-0011） |

多源指标（ADR-0017）：一条指标可有**一个主源 + N 个辅证**，裁决主源优先，辅证以原始形状保留在 `data` 里不丢、不加权合成：#2（静态行数主源 + 语义评审辅证）、#12（trace 报错主源 + `judges/deps.json` + 静态声明面 `deps_missing`，ADR-0031）、#4（六组静态规则 + 可移植性闸门主源 + `judges/pii.json` 语义辅证，ADR-0029）、#5（评审；消融实证随 T4 休眠）、#8/#9（共用一份 contract 评审）。
多源合取较差者：`worse(a, b)` 按 skipped → pass → warn → fail 的顺序取较差的一条（#4 用）。

## 沙箱

沙箱 = **git worktree**。被测 skill 的副本仓库上 `git worktree add`，运行在 worktree 内进行，评估产物不落回副本主目录。未来若替换为 Docker，只改本节定义，SKILL.md 流程不变。

每次 run 前重置工作树（ADR-0018）：触发/沙箱 run 共用同一个 worktree 时，前一个 run 写下的文件会被后一个 run 读到（实测并发探针里，一条 run 写的 `scratch/scan_all.py` 被另一条 run 当成仓库内容分析）。跑之前 `git -C <worktree> clean -fdx`，或每个 run 单独 `git worktree add`。

prompt 不写沙箱外路径（ADR-0018/0027）：worktree 只限定 cwd，不限定 agent 的写权限——实测一条带真实仓库路径的探针 prompt 让 agent 走出 worktree，改了评估器仓库自己的 `README.md`（已回滚）。评测集里的 prompt 只能引用沙箱内路径；`evalset_check.py` 会把绝对路径 / `..` 逃逸报成 `out_of_sandbox`（clean=false）。

越界检测（ADR-0027）：跑 run 时带上 `--guard-repo <评估器仓库>`（可重复），trace 里会多出 `guard.escaped_writes`（run 前后 `git status` 的新增条目）；`--guard-revert` 才回退（未跟踪→删、已跟踪改动→`git checkout --`）。默认只报告，避免吃掉并行写入者的改动。

Windows 上杀 pi 子进程树用 `taskkill /T`（`_subprocess.py` 的硬依赖，仅在 Windows 生效；POSIX 走 `killpg`）。

权限边界（#4）：worktree 方案下只能做到静态扫描 + trace 审计（看 trace 里是否出现了不该有的命令），做不到强制拦截。扫描规则见 `scripts/static_check.py` 内的六组正则（ADR-0013/0029）：危险命令 / 硬编码凭据 / 注入指令 / 数据外发 / 混淆 / 个人信息。前四组命中即 fail，后两组仅 warn。

依赖全面性（#12 静态面，ADR-0031）：`static_check.py` 报「用了什么 vs 声明了什么」——`deps_used`（shebang 解释器 / python import / `subprocess` 字面量 argv[0]）、`deps_declared`（SKILL.md 正文 + reference.md + `requirements.txt` + `check-deps.sh` 里出现过的名字，**任意出现即算声明，含随口提到——偏宽，漏报方向**）、`deps_missing`（差集）。缺声明只把 #12 降到 warn。已知漏检：argv 由变量拼时取不到 argv[0]；声明面是同义词不敏感的子串匹配（“Python3” 与 “python” 会漏判）。宿主基座（python/bash/sh/cmd/powershell…）不算未声明。

已知漏检（如实告知，不要当成"扫过就是安全"）：自造格式的高熵 token、改写的语义注入、跨段拼接的注入、无读取动词的 `.env` 引用。混淆与个人信息类命中仅警告，需人工复核。PII 静态面的边界：只能认格式化的个人标识（邮箱/手机号/身份证/银行卡/公网 IP，带 Luhn 与证件校验位），自造示例（如真实号段格式的假号码）会误报、工号与客户名会漏报——后者靠 `judges/pii.md` 四项检查点补。

扫描口径（ADR-0012）：默认排除 `tests/`、`evalsets/`、`runs/`、`cache/`、`calibration/` 与扫描器自身源码（`--no-default-excludes` 关闭默认排除）；`--exclude <路径>` 的相对路径按 **skill 目录**解析；单行末尾加 `static-check:ignore` 可抑制命中，被抑制项记入 `ignored` 字段（不静默丢）。

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

相同内容重复上传 → 相同 hash → 结果落同一目录（幂等 #16）。

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

冻结前跑质量自检（ADR-0011）：`python scripts/evalset_check.py <evalset目录> --skill <skill目录>`，查 should prompt 照抄 description、组内近似重复、同句跨组冲突；命中则先改写再冻结，裁决落 `evalset.json`（clean=false → report.py 把 #10 降级为 warn）。

草稿生成：`python scripts/evalset_gen.py --skill <skill目录> --out <evalset目录> --count 10`（默认走 `llm`；只写草稿，不写 `meta.json`）。前端「评测集」面板可生成草稿、加载逐条审核并写 `reviews/review.json`；`report.html` 的「评测集审核」tab 保留。

## 运行场景

档位（tier）定义增量递进的评估深度（ADR-0033），入口参数 `--tier <t0|t1|t2a|t2b|t3>`（也接受 `T0`–`T3`），**默认 T2A**。高档复用低档产物：结果目录里各中间产物记录生成时的 skill content_sha256、评估器指纹与评审模型，一致则复用，不一致则作废重跑；`--rerun-t0` / `--rerun-t1` 强制重跑对应档，Web 对应“重跑基础检查 / 重跑文档评审”。低档先行、后续增量补跑（staged）不重做已完成档；T0/T1 产物持久缓存到 `.skill-eval-cache/t0t1/<key>/`（仓库根，gitignore；key = skill/evaluator/评审模型哈希），跨 run / 重启仍可复用，`--rerun-*` 才强制重算。T4 不开放（ADR-0032）。

| 档 | 场景 | 做什么 | 服务指标 |
|---|---|---|---|
| T0 | 静态检查 | static_check.py | #1 #3 #4 #2（行数主源，150 warn / 500 fail） |
| T1 | LLM 评审 | 逐项按 judges/ rubric 评审（4 并发） | #2（语义两项，辅证） #5 #6 #7 #8 #9 |
| T2A | 触发评测 | 三组 prompt 各跑一次，带 `--early-exit`（ADR-0007 修订、ADR-0018）：事件流出现**首次指向被测 SKILL.md 的工具调用**（渐进式披露下 = agent 决定加载 skill）即终止该次运行省 token——判定按**路径解析**（相对路径按同串 `cd` 目标/运行 cwd 解析），不是绝对路径子串；未出现该调用的 run 跑完整，其行为正是 precision 的证据；**triggered 由 `trigger_judge.py` 用 LLM 判定**，判定口径与 early-exit 同步：**加载即触发，不要求任务实际完成**，截断运行（回答为空）按已执行步骤裁决（trace_run.triggered 仅作粗筛），环境故障的 case 不计入 P/R 分母。**`#3 resolved=human` 时整档不跑**：#10 记 `skipped`（载体测不到“按 description 触发”，ADR-0018） | #10 |
| T2B | 真实场景 A/B | 用户提供 ≥1 例真实输入；同 cwd、同输入跑 golden（加载 skill）与 baseline（不加载）两臂；#11 报告样本数 n，n<3 时最高 warn；#12 用 golden 臂 trace + 静态声明面；真实输入存 `uploads/`，报告只回显哈希 + 标签（ADR-0033）。未提供输入 → 整档 skipped | #11 #12 |
| T3 | 主运行 Golden Run | 干净 worktree + 加载 skill，跑冻结 cases，采 trace | #13 成本 / #14 结果可验证 |
| T3 | 重复运行 ×N | 主运行重复 N 次，每次 trace 存档 | #15（组内）/#16 |
| T3 | 跨模型运行（可选，默认不跑） | 同批 cases 换 `--model` 再跑，trace 落 `models/<模型名>/`，model_robust.py 出一致率 | #15（跨模型） |

## 模型配置

T2+ 执行载体（harness）的模型三层优先级：

1. 命令行 `--model <provider/id>`（显式，最高）
2. 环境变量 `SKILL_EVAL_MODEL=<provider/id>`（会话级默认，推荐在评估开始前设置）
3. pi 自身的默认模型（兜底）

T1 评审走 `llm` 接口层（ADR-0034）：模型 `--model` > `llm.model` > `SKILL_EVAL_MODEL`；端点 `--base-url` > `llm.base_url` > `OPENAI_BASE_URL`/`ANTHROPIC_BASE_URL` > 官方端点；凭据 `--api-key` > `llm.api_key_env` 指定的**已存在**变量 > `llm.api_key_file` + `llm.api_key_provider`（如 pi 的 `auth.json`，密钥不写进 config）> 标准 `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`。页面字段优先于 config，config 优先于环境变量；页面 key 只用于本次运行，不落盘。`--backend pi` 可回退旧路径。评测集 AI 审核是唯一例外，见 § 评测集 AI 审核。

可选 `--thinking <off|minimal|low|medium|high|xhigh|max>` 控制思考档位。

可配参数统一在 `config.yaml`（ADR-0035），优先级 CLI > 现有 ENV > config.yaml > 代码默认。关键项：description 字符阈值 384 warn / 1024 fail、正文 150 warn / 500 fail（ADR-0037）、触发集每组最小条数 10、F1 0.7、逐 case 变异系数上限 0.5、幂等比例 0.5、触发集覆盖 0.6/0.8、跨模型相似度 0.5/0.8、触发判定分 0.5、args 上限 2000、UI 单元格 800 / 内联 400；重复运行 3 次、评审单发 1 次/并发 4（runner 读取）。既有 `SKILL_EVAL_TRIGGER_MIN` 仍可覆盖最小条数；`--exclude <路径>`（相对 skill 目录解析）、`--no-default-excludes`（static_check.py）仍是 CLI 开关；`--threshold` 仍可覆盖触发判定分。`JUDGE_SAMPLES=3` 的多数票流程不变（跑 3 次后 `judge_runner.py --merge`，ADR-0028）。

判据口径（容易被误读，单独列）：#11 = 三分量多数票（tokens/tool_calls/seconds 各一票，ADR-0026）；
#5 = 评审面（消融实证随 T4 休眠，ADR-0026）；#12 = 全部 golden trace 取并集，依赖信号分强弱档
（弱档要求报错里带含分隔符的路径，agent 自己写错相对路径不算，ADR-0025），另有静态声明面
（`deps_missing` 非空至少降为 warn，ADR-0031）；#10 = human 型不适用（ADR-0018）；
#4 = 六组静态规则 + 可移植性闸门与 `judges/pii.json` 取较差者（ADR-0029）。

## 执行载体与扩展点（ADR-0034）

`scripts/harness.py` 是执行载体适配层：接口 `run_case(prompt, cwd, ...)`，当前只实现 `pi`（内部调用 `trace_run.py`，逐 case 一次 `pi --mode json --no-session`），触发评测带 `--early-exit`。`code_agent` 留接口不实现，前端只显示 `harness.available` 里的项。**不使用 subagent 机制**。

## 前端与 runner（ADR-0036）

`scripts/runner.py` 是确定性编排：按 `--tier` 调 T0→T1→T2A→T2B→T3，最后 `report.py`；进度以 NDJSON 输出，`--results` 指定产物目录，`--real-input` 给真实输入对比。

`web/app.py` 是 stdlib HTTP 入口（只绑 `127.0.0.1`）：
- `POST /api/upload`：浏览器直接以 `application/zip` 流式上传（不再 base64），安全解压到 `web/runs/uploads/`，返回 skill 路径；拒绝 `..`/绝对路径/符号链接，限制 1GB 压缩包、2GB 解压体积、20000 个文件。
- `GET /api/llm/config`、`POST /api/llm/test`：返回非敏感默认配置 / 测试连接。
- `POST /api/run`：启动 runner；可带 `llm`（protocol/base_url/model/api_key）与 `real_input`。
- `GET /api/events?id=`：SSE 进度；`GET /api/report?id=`：报告 JSON；`GET /report?id=`：报告页。
- `GET /api/evalset/status|items`、`POST /api/evalset/generate|review`：评测集检查、生成草稿、逐条审核（写 `reviews/review.json` + `audit.jsonl`）。
- `POST /api/override`：支持一次提交多条裁决调整（`changes: [{metric,to}]`），统一写 `overrides.json` + `manual-overrides.jsonl`，重出报告。

前端不重算指标。

被测 skill 出现宿主环境硬编码（绝对路径、特定用户目录、.claude/.cursor 等他方生态路径）→ 可移植性闸门整体 fail（ADR-0008，static_check 的 `hardcoded` 字段）。<!-- static-check:ignore -->

## 规则

- 评估器目录内不得出现针对特定被测 skill 的脚本（#31）；历史运行证据存 `evalsets/<name>/provenance/`
- LLM 评审一律逐项计分：rubric 拆检查点，每项 1 分，score 由 items 推导（#11，judge_runner.py 强制校验）

## 中间产物契约

所有中间文件落盘在 `evalsets/<name>/results/<version>/`（与报告同目录）：

- `triggers.json`：`{"should": [bool...], "should_not": [...], "confusable": [...]}`，布尔值 = 该 prompt 是否实际触发
- `runs.json`：`[{"case", "tool_calls", "tokens", "seconds"}, ...]`，golden（有 skill）各次运行；`case` 可选，**全条目都带**时 score.py 产出 `cost_by_case`（#15 组内口径的数据源，ADR-0016）；缺一条即不产出
- `baseline.json`：T2B 的基线（无 skill）运行；T3 不写它（necessity 只来自 T2B）
- `t2b/runs.json` + `t2b/baseline.json`：T2B 两臂的原始统计；`t2b/score.json` 的 `necessity` 由 runner 合并进根 `score.json`
- `real-input.meta.json`：`{"sha256", "label"}`；正文存评估器 `uploads/real-input/<sha>.json`（gitignore，报告不回显）
- `trace-<序号>.json`：`{"steps": [{"tool", "args", "args_hash"}, ...], "answer", "tokens", "seconds", "triggered", "errors", "host": {"model", "pi", "platform"}}`（trace_run.py 落盘，--out；host 为宿主 pin，事后归因/复现用；离线 --events 重算时仅记 model 意图）
- `static.json`：static_check.py --out（脚本自写 UTF-8；stdout 同步回显。禁 shell 重定向——GBK 控制台会产 GBK 文件）。全部脚本入口接 `scripts/_console.py::fix()`：GBK 控制台下回显不可编码字符降级为替换符，不崩、退出码 0；`--out` 文件不受影响。含 #4 的六组（`dangerous`/`secrets`/`injection`/`exfil`/`obfuscation`/`pii`）+ 出站网络 `egress`（GET/内网/socket/ssh 都算，仅警告）、`hardcoded`、`excluded`/`ignored`（ADR-0012）、`scan_excluded_dirs`（ADR-0012：运行面之外）、`stale_refs`（依赖新鲜度，ADR-0009 延伸：SKILL.md 声明但包内不存在的路径引用，warning 级）、`deps_used`/`deps_declared`/`deps_missing`（依赖全面性，ADR-0031，warning 级）、`skill_md_body_lines`/`skill_md_body_warn_lines`/`skill_md_body_fail_lines`（#2 行数主源与两档阈值，ADR-0017：口径 = frontmatter 之后全文行数，含空行与代码块；旧 `skill_md_body_max_lines` 只作历史产物兼容读取，新产物不再输出）<!-- static-check:ignore -->
- `score.json`：score.py --out（同上）；含 `cost`（多次运行汇总）+ `cost_by_case`（逐 case，仅全条目带 case 时出现）+ `necessity`（T2B 合并进来；每分量带 `n`）
- `idem.json`：多次 idem.py --out 结果的聚合 `{"ratio", "idempotent", "pairs", "idempotent_pairs", "per_pair"}`
- `golden.json`：主运行的 golden trace（#12 的数据源之一）；同时存在 `traces/golden-*.json` 时，
  #12 取**全部** golden trace 的报错并集（`data.sources` 记录条数，ADR-0025）
- `traces/`：逐 run 的原始 trace；`--guard-repo` 时另带 `guard.escaped_writes`（越界写入，ADR-0027）
- `compare.json`：compare.py --aggregate 裁决（#14）；逐 case 文件在 `compare/`
- `actual/` + `expected/`：T3 每 case 的实际回答与期望（compare.py 输入）
- `overrides.json`：人工复核 `{metric: {from, to, reason, reviewer, at}}`；from/to 必须是 pass|warn|fail|skipped，报告按 from 匹配当前裁决后应用
- `manual-overrides.jsonl`：人工调整的追加审计日志（前端 `/api/override` 写入）
- `evalset.json`：evalset_check.py stdout（触发集质量，#10 降级依据）
- `model_robust.json`：model_robust.py stdout（#15 跨模型一致率）
- `models/<模型名>/trace-<序号>.json`：跨模型运行的逐 case trace（序号 = 评测集顺序，各模型必须一致）
- `meta.json`：`{"schema": "skill-eval/results-meta/1", "generated_at", "conclusion", "evaluator", "skill_fingerprint", "evalset_source", "metrics"}`（report.py 写入，ADR-0010）；已存在时**合并保留**人工填写的键（model/sandbox/skipped_reason 等），不静默覆盖
- `judges/<metric>.json`：judge_runner.py 校验通过的 LLM 评审输出，metric ∈ {brevity, redundancy, fallback, precheck, contract, deps, pii}（**不要**存成 judge-*.json 或放 results 根目录，report.py 只认 judges/<metric>.json）
- `raw/llm-judge/<metric>.json`：T1 直连 LLM 的 request/response（含 prompt，不含密钥）；`--backend pi` 时原始事件流仍存 `raw/judge-events/<metric>.events.jsonl`

`score.py <results目录>` 读同目录三件套；`idem.py <trace1> <trace2>` 出幂等比例。统计口径：均值/标准差/n/min/max（#13/#28）；必要性为 tokens/tool_calls/seconds 三分量各自提升率，每分量带 `n`（#11）。

## Judge 校准

`calibration/<metric>/<case>/skill/` 放最小被测包，`calibration/<metric>/<case>/expected.json` 放人工逐项标注（`{"items": {"检查点名": true/false}}`）。`scripts/calibrate.py --metric <name> --samples 3` 对同一 case 多次评审，算逐项一致率与标注准确率；任一项低于 `--min-agreement`（默认 0.8）退出码 1，结果写 `calibration/results/<metric>.json`（gitignore）。校准集是 rubric 的门禁，不是评估结论；当前种子集 brevity/precheck 各 8 例，目标扩到每项 20–30 例（10 pass / 10 fail / 5 边界）。

## LLM 评审

**调用口径（ADR-0020/0022/0034）**：每条 rubric 一次**独立单发**调用——prompt = rubric 全文 + 被测文件（`judge_run.py` 收集，默认上限 60k 字符），不携带编排 agent 的历史/探索上下文。T1 默认走 `llm` 接口层：请求不含 tools/tool_choice，结构上保证无工具；`--backend pi` 回退旧路径时要求 `--no-tools`。固定可重放：同一 prompt、单发、无工具；原始 request/response 存 `raw/llm-judge/`。默认单次；要更高可信度可手工跑 3 次后 `judge_runner.py --merge`（成本 ×3，需在报告注明口径）。

输出：结构化 JSON（`{items: [{name, pass, quote}], score, evidence, reason}`，evidence 必须引用原文；**score = pass 项数/总项数**，0~1 两位小数，judge_runner.py 强制校验）。rubric 文件在 `judges/`。

流程门禁：`scripts/judge_run.py --metric <name> --skill <dir> --out judges/<metric>.json` 默认走 `llm`（无 tools），校验失败把 errors 回给模型重产一次，再失败则该指标标 `skipped`。原始 request/response 存 `raw/llm-judge/<metric>.json`；`--backend pi` 时事件流里出现 `tool_execution_start` 则该次评审作废重跑（ADR-0022）。

`JUDGE_SAMPLES=3` 时：同一 prompt 跑 3 次 → `python scripts/judge_runner.py --merge <a.json> <b.json> <c.json> --out judges/<metric>.json`
（逐项多数票，分歧项与各次得分留在输出里，ADR-0028）。

检查点极性：每个 rubric 的检查点分两向——**能力清单**（做到才 pass，如 precheck 的环境校验置前）与**问题清单**（无此类问题才 pass，如 deps 的无未打包外部依赖）。极性逐条定义，同一 rubric 内可混向（实际也混）；items 契约与 score 公式不感知极性，新写 rubric 时只需保证每条检查点的 pass 条件在文案里可判定，不需要把措辞统一成一向。

后续可切换为 3 次取中位数或多模型交叉——切换时只改本节，rubric 不动。

## 幂等（#16）

重复运行的 trace 两两跑 `idem.py`：第二次重复第一次已完成步骤的比例 > 0.5 → 不幂等，进报告 `#16` 项。

## 稳定性与跨模型（#15）

#15 有两个独立信号，报告里分写不合并（ADR-0011）：

- **组内**：重复运行 ×N 的 tool_calls 变异系数 ≤ COST_CV_MAX(0.5) → 稳定；n < 2 时只记"无稳定性证据"
- **跨模型**：`model_robust.py` 的一致率 ≥ AGREEMENT_MIN(0.8) → 鲁棒；默认不跑（成本 ×模型数）

裁决口径保守：跨模型只能**降级**（发现漂移 → warn），不能升级——它不能代替组内重复运行。
case 一致 = triggered 全同 且 回答相似（字符 3-gram Jaccard ≥ 0.5；全空回答视为无信息，early-exit 截断不算不一致）。

## 报告契约

```
evalsets/<name>/results/<version>/
├── report.json   # schema=2 + legacy_id_map + tier + 16 个 key=指标名，值={verdict: pass|warn|fail|skipped, method, data, note}
│                 # 顶层另带 advice（优化建议数组）、evaluator（评估器指纹）、reviewed_by 与 generated_at，不占指标 key
├── report.md     # 每条指标一节：表格化的数据 + 证据；末尾「## 优化建议」段（顶部显示档位/审核口径/评估器指纹）
├── report.html   # --html：report.md 的渲染媒介 + 优化建议 tab + 评测集审核界面；报告 tab 不得比 report.md 少信息
└── meta.json     # {"schema": "skill-eval/results-meta/1", "generated_at", "conclusion",
                  #  "evaluator", "skill_fingerprint", "evalset_source", "metrics", tier}（report.py 写入；
                  #  已存在时合并保留人工字段：model/sandbox/skipped_reason 等，ADR-0010）
```

规则：
- `tier` 记录本次跑到的最高档位；`tier_label` 是给人看的名称；`metric_tiers` 给出 #N→T0/T1/T2A/T2B/T3 分组
- 跨档位的指标值不直接比较
- 各中间产物记录生成时的 skill content_sha256；高档复用低档产物时校验一致，不一致则该产物作废重跑
- 16 条指标全部出现，`metrics` 按 #1–#16 排序；未测的标 `skipped` 并注明原因，不打 0 分
- `manual_overrides` 列出本次人工调整的指标、原值/新值、原因、审核人与时间；报告 note 同步标注
- **data 渲染**（report.md / report.html 共用一套，ADR-0030）：**所有指标一律表格化**——已知形状走定制表（#16 `per_pair`、#15 `cost_by_case`+`cv_by_case`、#13 `cost`、#14 `per_case`、#10 三率、#11 三分量、#12 报错与评审项、#4 六组与闸门），其余走**通用兜底**（dict/list 递归压平成 key-value 表）；judge 面（`items`）另加 score/reason 摘要行。原始 JSON 一律保留：**有表就折叠**（`<details>`，不管多短），无表且 ≤400 字符才内联；单元格 >800 字符截断并标注。**不得静默截断**（旧实现 `[:2000]` 会把 2165 字符的 #16 data 切成非法 JSON）。渲染层不得反向改 `report.json` 的形状
- **结果符号**：卡片徽章、表格结果列与顶部 16 行总览表（`id="overview"`）统一 pass=✓ / fail=✗ / warn=⚠ / skipped=–
- **优化建议**（ADR-0030）：`report.json.advice` = `[{metric, severity, problem, evidence, action}]`，由 `build()` 从裁决确定性派生（`severity` = fail/warn/info，排序 fail→warn→info）；每个指标卡下方内联该指标建议，`report.html` 另有「优化建议」tab（`#N` 锚点跳回指标卡），report.md 有 `## 优化建议` 段。问题与建议同源，不存在“建议里说了、指标里没有”
- **可移植性闸门可见性**（ADR-0008/0030）：`static.json.hardcoded` 非空 → #4 裁决 = fail（闸门不属于独立指标，只能挂在 #4 上才进得了 `conclusion()`）；`stale_refs` / `scan_excluded_dirs` 只作 #4 卡的信息行，不改裁决
- 评测集 `reviewed_by: "ai"` 时，报告与 #10/#11/#14 的结论处注明「评测集 AI 审核，未经人工复核」
- 不合成加权总分；结论分三档：必测全过 / 有警告 / 有失败
- `report.json` 顶层带 `schema: 2` 与 `legacy_id_map`；旧报告不重写、不迁移（ADR-0032）
- `report.py <results目录> --skill <被测skill目录> [--evalset <evalsets/<name>/vN>]`：`--skill` 让 meta.json 记下被测 skill 的内容指纹——vN 目录命名下唯一能看出"评的是哪份内容"的来源
- 评估器指纹或 report schema 不一致的版本之间，指标差异不构成回归证据

## Fallback

| 故障 | 处置 |
|---|---|
| 被测路径无 SKILL.md | 终止，报告"不是合法 skill 包" |
| scripts/judges 缺失 | 报告评估器自身损坏，列出缺失项，终止 |
| 硬依赖缺失（`bash scripts/check-deps.sh` 报 FAIL） | 终止：缺 python≥3.9/git 时会静默降级，先装齐再跑 |
| 缺 pi CLI 或模型（自检仅 warn） | 只能跑静态检查；沙箱类指标标 `skipped`，报告注明原因 |
| 未配置 `llm.base_url`/模型 | T1 `--backend auto` 回落 pi；显式 `--backend llm` 报错，该指标 skipped |
| T2B 未提供真实输入 | #11/#12 标 `skipped`，不自动生成“真实”输入 |
| worktree 创建失败 | 换临时目录 + 提示隔离降级，继续跑 |
| 沙箱运行崩溃 | 降级（fallback）到最近一个已完成的低档：报告标注缺失指标与原因，不静默丢指标 |
| 审核模型不可用 | 评测集保持未冻结状态，提示人工审核路径，不阻塞已完成的低档位报告 |
| 评测集未审核 | #10/#11/#14 标 `skipped`，其余照常 |
| 触发集未做质量自检 | #10 只按 F1 判（不降级），meta.json 里评测集来源照记 |
| 跨模型未跑 | #15 只出组内信号，不扣分 |
| `disable-model-invocation: true`（#3=human） | #10 标 `skipped`：不跑触发评测（ADR-0018）；已实测的数据留在 `data` 里作证据 |

## T4 休眠

`process.py`（过程审计）、`ablation.py`（消融）、`evolution.py`（版本演进）与 `judges/side-effects.md` 已移入 `attic/`，不随包分发、不跑测试；将来 T4 回归再启用（ADR-0032）。

## 可信度工具（ADR-0010 / ADR-0011 / ADR-0016）

- `scripts/evalset_count.py <evalsets/<name>/vN>`：冻结前置的**条数闸门**——三组触发集各 ≥ 最小条数（默认 10，可配），并提示 `meta.json counts` 与实际的差异；不达标不进入审核
- `scripts/evalset_check.py <evalset目录> --skill <skill目录>`：#10 可信度——3-gram 覆盖率查照抄/重复/冲突，裁决落 `evalset.json`（`clean=false` → report.py 把 #10 降级为 warn）
- `scripts/model_robust.py <results目录>`：#15 跨模型一致率——读 `models/<模型名>/trace-<序号>.json`，裁决落 `model_robust.json`；单模型或无 `models/` 目录 → `skipped`
