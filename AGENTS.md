# PROJECT KNOWLEDGE BASE

Generated: 2026-09-22T13:43:08.578Z
Snapshot: 2026-09-24 · 基准 commit `7d6b6e5` · Branch: main
最近一次在分支 `feat/report-advice-and-pii` 上做的更新（ADR-0029/0030/0031）：
计数与结构已重算（321 tests / 37 ADR / 16 个 Python CLI + 1 个共享模块 + 9 份 rubric）。v2 设计（ADR-0032–0037）已落文档，代码按 PR1–PR5 分批。

## OVERVIEW

`skill-evaluator` —— 一个评估其他 skill 的 pi skill 包，按 19 条指标产出 `report.json` + `report.md`。不是服务，不是 Python 库：复制到 skills 目录，指向任意 skill 包即可跑。

仓库 ≠ 产品。**230 个文件里只有 53 个被 git 跟踪**，其余全是 `evalsets/**` 下的评测集与历史运行产物，被 `.gitignore` 排除。

## STRUCTURE

```
skill-evaluator/        # 产品本体（自闭环，ADR-0006）
├── SKILL.md            # 编排：五档流程（T0–T4，默认 T2）+ 9 步，只放立即要做的事
├── reference.md        # 契约全集：指标映射/沙箱/版本号/中间产物/fallback
├── requirements.txt    # 只声明 pytest>=7；运行时零第三方依赖（stdlib）
├── LICENSE             # MIT
├── config.yaml         # 可调阈值/常量（ADR-0035），参与评估器指纹
├── scripts/            # 16 个 Python CLI + _config.py 共享模块 + check-deps.sh 前置自检，扁平单层
├── judges/             # 9 份 LLM 评审 rubric（md）
└── tests/              # 321 个测试，扁平单层；conftest.py 只设 PYTHONIOENCODING
docs/adr/               # 37 条架构决策（0001–0037）
evalsets/               # ⚠ 被 gitignore：评测集 + 运行产物，本地态```

非显然点：

- `docs/` 是空壳，内容只在 `docs/adr/`。
- `evalsets/` 有两处：根级（被测 skill 的集）与 `skill-evaluator/evalsets/`（评估器自己的集）。**两处都不入库。**
- 无 `pyproject.toml` / `setup.py` / `Makefile` / `.github/` / `conftest.py` / `pytest.ini` / `__init__.py`。`requirements.txt` 只声明 `pytest>=7`——**运行时零第三方依赖**，但需 **Python ≥ 3.9**（`Path.is_relative_to`）+ git。外部能力依赖 pi CLI。
- `scripts/` 内部靠 `sys.path.insert(0, Path(__file__).parent)` 做同目录导入，必须以脚本目录为基准调用。
- `scripts/check-deps.sh` 是 #16 前置自检（退出码 0/1），但**不在评估器指纹范围内**——指纹只收 `scripts/*.py` + `judges/*.md` + `config.yaml` + `SKILL.md` + `reference.md`（`report.py:94`），改 .sh 不会让历史报告变 `comparable=false`。

## WHERE TO LOOK

粗粒度路由：进哪个目录。目录内的细粒度定位见各自的 `AGENTS.md`。

| 我要…… | 去哪 |
|---|---|
| 改阈值/常量（384/1024 字符、150/500 行等） | `skill-evaluator/config.yaml`（ADR-0035）；解析器在 `scripts/_config.py` |
| 改评估逻辑、指标阈值、扫描规则 | `skill-evaluator/scripts/` → 先读该目录 `AGENTS.md` |
| 改 LLM 评审标准 | `skill-evaluator/judges/<metric>.md` |
| 改契约（指标映射 / 沙箱 / 版本号 / 中间产物 / fallback） | `skill-evaluator/reference.md` |
| 改五档流程本身 | `skill-evaluator/SKILL.md` |
| 加测试 | `skill-evaluator/tests/` → 先读该目录 `AGENTS.md` |
| 加/改架构决策 | `docs/adr/` → 先读该目录 `AGENTS.md` |
| 搞懂术语（skill / 指标 / 场景 / 评测集 / 指纹） | `CONTEXT.md` 词汇表 |
| 看历史评估结果 | `evalsets/**/results/<version>/`（本地态，不入库） |
| 跑一次评测 | `skill-evaluator/SKILL.md § 流程` + 本文件 § COMMANDS |

## CODE MAP

实测引用度（grep 命中）。**只有三条跨模块代码级依赖**：

| 符号 | 位置 | 引用 | 职责 |
|---|---|---|---|
| `report.build` | `report.py:298` | 文件内 | **19 指标的汇聚点**：所有上游产物的唯一消费处；同时派生 `advice`（优化建议） |
| `report.from_static` | `report.py:188` | 文件内 | 静态面 → #2/#7/#13；#13 六组 + 可移植性闸门在此合成裁决 |
| `report._advice` | `report.py:970` | 文件内 | 从裁决确定性派生优化建议（ADR-0030）——不得改成 LLM 生成 |
| `report._tables` | `report.py:1033` | 文件内 | data → 表格列表（定制表 → 通用兜底），ADR-0030 |
| `compare.extract_answer` | `compare.py:22` | 9（扇入 2） | 事件流 → JSON 裁决提取；被 `process.py:17`、`trigger_judge.py:24` 复用 |
| `static_check.parse_frontmatter` | `static_check.py:327` | 4（扇入 1） | 被 `evalset_check.py:31` 复用 |
| `static_check.collect_deps` | `static_check.py:279` | 文件内 | 依赖全面性：用了什么 vs 声明了什么（ADR-0031） |
| `trace_run.EventAggregator` | `trace_run.py:19` | 7 | pi 事件流聚合 + early-exit |
| `report.evaluator_fingerprint` | `report.py:94` | 7 | ADR-0010 指纹，测试直调 |

其余 13 个脚本**不被任何脚本 import**，只作为 CLI 子进程被调用——改它们的内部实现不破坏调用方，改 stdout JSON 契约会。

## CONVENTIONS

- **语言双轨**：`CONTEXT.md`、`skill-evaluator/*.md`、`docs/adr/*.md` 全中文；`README.md` 英文 + `README_CN.md` 中文镜像成对，改一侧必须改另一侧。
- **指标写作 `#N`**（#1–#19），贯穿文档与代码；裁决值恒为 `pass|warn|fail|skipped`。
- **相对路径基准 = 被测 skill 目录**，不是 CWD（ADR-0006/0012）。评估器自身即 `skill-evaluator/`。
- **配置只有两层**：CLI 参数 > 环境变量 `SKILL_EVAL_MODEL` > pi 默认模型。阈值常量内置于脚本头部，注释标"可配"（ADR-0009）。
- **报告契约**：19 个 key 恒定出现，未测标 `skipped`（**绝不打 0**），不合成加权总分，只三档结论（ADR-0004）。
- **报告呈现**（ADR-0030）：所有指标 data 一律表格化（定制表 → 通用兜底），有表就把原始 JSON 折叠；结果符号 pass=✓/fail=✗/warn=⚠/skipped=–；`report.json.advice` 与指标裁决同源（每条带 `#N` 反链），`report.html` 有「优化建议」tab。
- **可移植性闸门可见**：`static.json.hardcoded` 非空 → #13 裁决 = fail（ADR-0008/0030）。闸门不占独立指标，只能挂 #13 才进得了 `conclusion()`。
- **评估器指纹** = `scripts/*.py` + `judges/*.md` + `config.yaml` + `SKILL.md` + `reference.md` 的内容 sha256（路径也参与），形如 `auto-3f9c1a7b`（ADR-0010）。**不收 `.sh`、不收 `CONTEXT.md`/README**——改这些不会让历史报告变不可比。
- **运行时需 Python ≥ 3.9**（`Path.is_relative_to`）+ git（沙箱用 worktree）。`requirements.txt` 里的 `pytest>=7` 仅跑测试用。
- 决策走 `docs/adr/`：新编号新文件，不改旧 ADR。

## ANTI-PATTERNS

跨目录生效的硬禁令。每条都有仓库实证：

- **不 `git add .`**：`.gitignore:3` 的 `evalsets/` 匹配任意层级；`uploads/`、`prompt.txt`、`scratch/`、`wt-*/` 同为临时/敏感产物。误提交即把评测集与上传物发布出去。
- **不重新生成已冻结的评测集**：冻结集跨版本复用是 #14 可比的前提（ADR-0002）。自动生成的集未经人工确认不得写 `meta.json`，且不得进入第 5 步（`SKILL.md:22`）。
- **不照抄 description 写 `should` prompt**（ADR-0002/0011），否则 precision 虚高。
- **不用 subagent 或 self-simulation 代替 pi CLI 子进程**跑沙箱（ADR-0007 明写"禁止 self-simulation"）。
- **不写死机器路径**，包括解释器路径（统一 `sys.executable`）——命中即触发可移植性闸门整体 fail（ADR-0008/0009）。
- **不把 LLM 评审存成 `judge-*.json` 或放 results 根目录**：`report.py` 只认 `judges/<metric>.json`（`reference.md:104`）。本仓历史产物里有 5 个 `judge-*.json` 反例，别照抄。
- **不用 0 填充未测指标**，不合成加权总分（ADR-0004）。
- **不把可数事实交给 LLM 判**：#3 的行数固定走 `static_check.py`（`skill_md_body_lines` 主源），rubric 只留语义两项；要改阈值就改 `SKILL_MD_BODY_MAX_LINES`，不要在 rubric 里重写一条（ADR-0017）。
- **不改 #13 的命中分级**：六组静态规则中 `dangerous`/`secrets`/`injection`/`exfil` 命中即 fail，`obfuscation`/`pii` 只是 warn（待人工复核）——把混淆或个人信息也提成 fail 会误杀合法示例（ADR-0013/0029，`report.py:188`）。
- **不让缺声明把 #8 判成 fail**：`deps_missing` 只降到 warn（ADR-0031）——缺声明不影响运行，fail 会与 trace 面“跑得通”的证据打架。
- **不用 LLM 生成优化建议**：建议必须从裁决确定性派生（`report.py:970`），否则建议与指标脱钩（ADR-0030）——那正是这次要修的问题。
- **不在 ADR 里写操作步骤**，也不把决策重复进 `SKILL.md`（ADR-0001 定义单点）。

## COMMANDS

无安装步骤（运行时零第三方依赖）。硬依赖：Python ≥ 3.9、git；能力依赖：pi CLI；测试依赖：pytest。

```bash
cd skill-evaluator                    # 所有相对路径以此目录为基准

bash scripts/check-deps.sh            # 前置自检，退出码 0 = 可跑（缺硬依赖不要继续）
pip install -r requirements.txt       # 仅跑测试时需要（pytest>=7）

python -m pytest tests                # 全量：321 个测试
python -m pytest tests/test_idem.py   # 单文件
python -m pytest tests -k "idem or report"

python scripts/static_check.py skill-evaluator   # 评估器自检，必须输出 passed: true
```

完整评估流水线（9 步 / 五档，`<VER>` = 版本号）：

```bash
bash scripts/check-deps.sh                                    # 1 前置自检
python scripts/static_check.py <被测skill目录>                  # 3 静态检查（六组规则 + 可移植性闸门 + 依赖全面性）
python scripts/evalset_count.py evalsets/<NAME>/v1              # 4a 条数闸门（三组各 ≥10）
python scripts/evalset_check.py evalsets/<NAME>/v1 --skill <被测skill目录>  # 4b 触发集质量自检
python scripts/trace_run.py <worktree> "<prompt>" --skill <被测skill目录> --early-exit --out results/<VER>/trace-1.json
python scripts/trigger_judge.py --skill <被测skill目录> --trace results/<VER>/trace-1.json --out results/<VER>/triggers.json
python scripts/judge_runner.py --validate results/<VER>/judges/<metric>.json
python scripts/score.py results/<VER>
python scripts/idem.py results/<VER>/trace-1.json results/<VER>/trace-2.json
python scripts/compare.py --expect <expect.txt> --actual <actual.txt> --out results/<VER>/compare.json
python scripts/process.py --skill <被测skill目录>/SKILL.md --trace results/<VER>/golden.json --out results/<VER>/process.json
python scripts/report.py results/<VER> --skill <被测skill目录> --evalset evalsets/<NAME>/v1
python scripts/evolution.py evalsets/<NAME>        # #14，多版本时
```

## NOTES

- **最大的坑**：`.gitignore:3` 的 `evalsets/` 会忽略 `skill-evaluator/evalsets/`。README / ADR-0002 / 0006 声称冻结集持久化在那里，但它们从不入库——clone 或复制到 `~/.pi/agent/skills/` 后集就丢了，#14 跨版本对比的前提被破坏。要么把 `.gitignore` 改成 `/evalsets/`，要么接受集是本地态。
- **仓库在生成本文件时仍在被编辑**（历史快照注记，已过时）：2026-09-22 21:47–21:48 的另一个写入者新增了 `requirements.txt`、`LICENSE`、`scripts/check-deps.sh`、`docs/adr/0013`。该批次已全部入库，本节仅供追源。
- **工作区已清空**（相对 commit `33166d4` 的改动均已入库）：`8711b78` 收了 #13 五组规则 + `report.py` 回显，`f5f0213` 收了 `LICENSE`/`requirements.txt`/`check-deps.sh`/frontmatter 三字段与 README 计数；同步到远端后又进来 34 个 commit（五档/组内稳定性/ADR-0014–0028/`_console.py`/`_subprocess.py`/`evalset_count.py`/`tests/conftest.py`）。
- ~~`skill-evaluator/tests/tmp-ev/triggers/should/s1.json`~~：误提交的 scratch 残渣已在 `a6a2a3` 之后的提交里删除（`git diff d26a6b6 HEAD` 显示 `D tests/tmp-ev/...`），且 `tmp-ev/` 已进 `.gitignore`。
- **第二大坑（本次实测）**：新建 worktree 里 **`skill-evaluator/evalsets/` 不存在**（gitignore 的本地态目录不随检出走），而 `tests/test_static_check.py::test_evaluator_self_check_passes` 断言 `excluded` 里含 `evalsets`——worktree 里不先 `mkdir -p skill-evaluator/evalsets` 就会挂 1 个测试。clean clone 同样中招，这是测试对本地态的隐性依赖（未修）。
- **两个 evalset 脚本别混**：`evalset_count.py` = 冻结前的条数闸门（三组各 ≥ 最小条数）；`evalset_check.py` = 触发集质量自检（照抄 description / 组内重复 / 跨组冲突，`--skill` 必填）。合并时后者保留了已发布的名字。
- `report.py` 是最大脚本（1307 行）：`build()` 是约 260 行 if 链，19 指标无子函数拆分；`_HTML_TMPL` 是约 100 行内联 HTML+JS（含审核与建议 tab）。改报告逻辑先读 `build()`，再读 `_tables()`/`_advice()`（ADR-0030 的呈现层）。
- `normalize()`/`grams()` 在 `evalset_check.py` 与 `model_robust.py` 各有一份字节级相同的实现；pi CLI 调用块在 `compare.py`/`process.py`/`trigger_judge.py` 重复三份。抽公共模块前注意：评估器指纹会让所有历史报告变 `comparable=false`（ADR-0010）。
- **已修复，勿再当缺口**：`report.py` 现在会回显 `excluded`/`ignored`（ADR-0012）；`secrets` 组带 `PLACEHOLDER_RE` 占位符过滤；**可移植性闸门不再隐形**（`hardcoded` 非空 → #13 fail，ADR-0030）；**原生 JSON 不再裸奉在报告页**（19 条指标全表格化 + 折叠，ADR-0030）；`pii` 组的示例白名单不会对 `example.com`/`13800138000`/测试卡号误报（ADR-0029）。
- **本轮（ADR-0029/0030/0031）改写了 1 个旧断言的语义**：`test_report3.py` 里“无表可渲染的长 data 只留 `report.json` 指针”是旧契约，被 ADR-0030 取代为“一律表格化”（已就地改写并注释，不是默默改测试）。
