# PROJECT KNOWLEDGE BASE — skill-evaluator/scripts

Generated: 2026-09-22T13:43:08.578Z

## OVERVIEW

13 个确定性 Python CLI + `check-deps.sh` 前置自检，扁平单层，共约 2400 行。每个脚本 = 一个有文档的 CLI seam：输出 stdout JSON 或落盘中间产物。脚本之间无 subprocess 调用。

## WHERE TO LOOK

| 脚本 | 行数 | CLI seam | 服务指标 |
|---|---|---|---|
| `report.py` | 583 | `<results目录> [--html] [--evalset <vN>] [--skill <dir>]` | 汇总 19 条 |
| `static_check.py` | 297 | `<skill目录> [--exclude <p>]... [--no-default-excludes]` | #2/#7/#13 五组 + 可移植性闸门 |
| `trace_run.py` | 245 | `<worktree> <prompt> [--skill\|--no-skill] [--early-exit] [--model] [--events] [--out] [--build-only]` | #1/#4/#5/#8/#12/#15 数据源 |
| `evalset_check.py` | 180 | `<evalset目录> --skill <skill目录> [--out]` | #1 可信度（触发集质量） |
| `evalset_count.py` | 111 | `<evalsets/<name>/vN> [--min*] [--out]` | #1 冻结前条数闸门 |
| `model_robust.py` | 173 | `<results目录> [--out]` | #12 跨模型 |
| `compare.py` | 145 | `--expect <f> --actual <f> [--aggregate <dir>] [--model] [--events] [--out] [--build-only]` | #9 |
| `evolution.py` | 127 | `<evalsets/<name>> [--out]` | #14 |
| `process.py` | 105 | `--skill <SKILL.md> --trace <trace.json> [--model] [--events] [--out] [--build-only]` | #10 |
| `trigger_judge.py` | 101 | `--skill <SKILL.md> --trace <trace.json> [--model] [--events] [--out] [--build-only]` | #1 触发判定 |
| `score.py` | 94 | `<results目录>` | #1/#4/#5 |
| `ablation.py` | 82 | `--skill <dir> [--delete "t1,t2" \| --list] --out <dir>` | #6 实证 |
| `judge_runner.py` | 67 | `--validate <judge输出.json>` | rubric 契约门禁 |
| `idem.py` | 41 | `<trace1.json> <trace2.json>` | #15 |
| `check-deps.sh` | — | `bash scripts/check-deps.sh`（退出码 0/1） | #16 前置自检，非 Python |

## SYMBOL MAP

顶层函数 + 行号。

- `report.py` — `load`(40) `load_judge`(59) `_sha256_files`(67) `evaluator_fingerprint`(77) `skill_fingerprint`(98) `evalset_meta`(116) `build_meta`(133) `write_meta`(151) `verdict_of_ratio`(162) `verdict_of_score`(167) `from_static`(171) `build`(207) `conclusion`(336) `_esc`(357) `_flow_svg`(362) `_evalset_html`(378) `render_html`(499) `render_md`(528) `main`(547)；常量 `DIM_13_GROUPS`(29)
- `trace_run.py` — `EventAggregator`(19, class；`.feed`(30) `.result`(66)) `parse_events`(88) `build_args`(101) `main`(124)
- `static_check.py` — `parse_frontmatter`(125) `token_estimate`(137) `scan_patterns`(144) `resolve_excludes`(172) `default_excludes`(181) `rel_paths`(190) `main`(201)；常量 `DANGEROUS_PATTERNS`(45) `SECRETS_PATTERNS`(61) `PLACEHOLDER_RE`(72) `INJECTION_PATTERNS`(78) `EXFIL_PATTERNS`(93) `OBFUSCATION_PATTERNS`(107) `HOST_HARDCODE_PATTERNS`(117)
- `evalset_check.py` — `normalize`(35) `grams`(40) `coverage`(47) `load_group`(55) `find_duplicates`(75) `find_conflicts`(87) `check`(98) `main`(151)
- `model_robust.py` — `normalize`(34) `grams`(38) `jaccard`(45) `load_model_dir`(52) `mean`(71) `analyze`(75) `main`(150)
- `compare.py` — `build_prompt`(15) `extract_answer`(22) `main`(52)
- `evolution.py` — `version_key`(16) `collect`(31) `evaluator_versions`(47) `comparability`(64) `main`(77)
- `process.py` — `build_prompt`(18) `errors_placeholder`(34) `main`(38)
- `trigger_judge.py` — `build_prompt`(17) `main`(29)
- `score.py` — `load_json`(16) `prf1`(24) `stats`(40) `necessity`(53) `main`(75)
- `ablation.py` — `split_sections`(10) `main`(28)
- `judge_runner.py` — `score_of_items`(14) `validate`(20) `main`(52)
- `idem.py` — `load_trace`(12) `main`(22)

## CONVENTIONS

- **同目录导入靠 `sys.path.insert(0, Path(__file__).parent)`**（每个 `main()` 里）。因此脚本必须以 `scripts/` 为基准被调用。
- **argv 手写解析**（`while i < len(args)`），不用 argparse。11 个脚本各写一遍——加参数时跟随既有写法。
- **stdout 契约字段恒输出**：输入缺失或非法 → 输出 `"skipped"`，**不崩溃、不打 0 分**（`score.py`、`report.py` 均如此）。
- **LLM 调用块统一形态**：`cmd = ["pi", "-p", "--no-session", "--no-skills"]`，`shutil.which("pi") or "pi.cmd"`，`timeout=300`。
- **离线 seam 三件套**：`--build-only`（只打印将执行的命令）、`--events <jsonl>`（离线解析已采集事件流）、`--out <file>`。测试全靠它们，改这三个 flag 会同时打断多个测试。
- 阈值常量放文件头部并注明"可配"：`IDEMPOTENT_MAX_RATIO=0.5`、`F1_PASS=0.7`、`COST_CV_MAX=0.5`、`ECHO_COVERAGE_MAX=0.6`、`DUP_COVERAGE_MAX=0.8`、`ANSWER_SIM_MIN=0.5`、`AGREEMENT_MIN=0.8`。

## ANTI-PATTERNS

- 不用 argparse / click 重写 argv 解析。
- 不让脚本 A subprocess 调脚本 B：串联只靠 `results/<version>/` 下的产物文件（唯一子进程是 pi CLI）。
- 不改 stdout JSON 的字段名、不新增必填字段——`report.py` 按名读中间产物，改名即断链。
- 不在 `main()` 之外做 IO：模块顶层不读写文件，保证 import 无副作用（`tests/test_trace_run.py:179` 会直接 import 模块）。
- 不硬编码解释器或机器路径，一律 `sys.executable`（ADR-0009）。
- `DANGEROUS_PATTERNS` 里的 `su`+`do` 拼接（`static_check.py:65`）是合并前另一条线的写法，与 ADR-0012 的自排除**并存**；两条都保留，别只删其一（删自排除会让自检失败）。
- 不把 `SECRETS_PATTERNS` 的占位符过滤（`PLACEHOLDER_RE`，`static_check.py:72`）删掉：文档里合法写 `api_key="your-key-here"` 会被误报为硬编码凭据（ADR-0013）。
- 不把 `OBFUSCATION_PATTERNS` 的裁决提到 fail：它只能 warn，命中多为打包/压缩产物（ADR-0013，`report.py:186-203`）。

## NOTES

- 重复实现（抽公共模块前先读根 `AGENTS.md` 关于指纹的警告）：`normalize()` `evalset_check.py:35` vs `model_robust.py:34`（字节级相同）；`grams()` `:40` vs `:38`；`coverage()`(`:47`) 与 `jaccard()`(`model_robust.py:45`) 同源但分母不同（非对称覆盖 vs 并集）。
- 代码级 import 扇入只有三条：`compare.extract_answer` ← `process.py:15`、`trigger_judge.py:14`；`static_check.parse_frontmatter` ← `evalset_check.py:26`；`trace_run.EventAggregator` ← `tests/test_trace_run.py:179`。其余 9 个脚本零代码级依赖。
- `report.py:207 build()` 是约 130 行 if 链，19 指标无子函数。读它反推所有上游契约是最快路径。
- `report.py:430 _HTML_TMPL` 是约 70 行内联 HTML + 前端 JS（含评测集审核交互）。改 HTML 时注意 JS 字符串里的换行会被截断（历史 bug：commit `0ef26f5`）。
- 评估器指纹只收 `scripts/*.py`（`report.py:77`）——`check-deps.sh` 改了**不会**让历史报告变 `comparable=false`，改 `.py` 会。
- 新增 `check-deps.sh` 的坑：Windows 上 `python3` 可能是应用商店占位符，`command -v` 能找到但跑不起来，必须实跑验证再采纳（`check-deps.sh` 已这么写，别简化成 `command -v python3`）。
