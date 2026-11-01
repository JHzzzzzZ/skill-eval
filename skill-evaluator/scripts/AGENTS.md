# PROJECT KNOWLEDGE BASE — skill-evaluator/scripts

Generated: 2026-09-22T13:43:08.578Z

## OVERVIEW

16 个确定性 Python CLI + 5 个共享模块（`_config`/`_console`/`_subprocess`/`llm`/`harness`）+ `check-deps.sh` 前置自检，扁平单层。另有 3 个 T4 脚本在 `attic/scripts/`（不进包、不跑测试）。每个脚本 = 一个有文档的 CLI seam：输出 stdout JSON 或落盘中间产物。测量脚本之间无 subprocess 调用；runner/harness 是编排层例外。

## WHERE TO LOOK

| 脚本 | 行数 | CLI seam | 服务指标 |
|---|---|---|---|
| `_config.py` | 204 | 无 CLI；`get/get_int/get_float/pick` | config.yaml 读取（ADR-0035） |
| `llm.py` | 184 | 无 CLI；`chat/ask/configured/extract_json` | T1 直连 LLM（ADR-0034） |
| `harness.py` | 75 | 无 CLI；`run_case/available` | 执行载体适配（当前 pi） |
| `judge_run.py` | 172 | `--metric --skill --out [--backend]` | T1 评审执行器 |
| `evalset_gen.py` | 115 | `--skill --out [--count]` | 生成 T2A 评测集草稿 |
| `runner.py` | 781 | `--skill --tier [--evalset] [--real-input]` | 确定性编排 + NDJSON 进度 |
| `report.py` | 1442 | `<results目录> [--html] [--evalset <vN>] [--skill <dir>] [--tier <t>]` | 汇总 16 条 + 优化建议 + 报告页 |
| `static_check.py` | 714 | `<skill目录> [--exclude <p>]... [--no-default-excludes]` | #1/#3/#4 六组 + 可移植性闸门 + #12 依赖全面性 |
| `trace_run.py` | 474 | `<沙箱目录> <prompt> [--skill\|--no-skill] [--early-exit] [--model] [--events] [--out] [--build-only]` | #10/#13/#11/#12/#15/#16 数据源 |
| `evalset_check.py` | 180 | `<evalset目录> --skill <skill目录> [--out]` | #10 可信度（触发集质量） |
| `evalset_count.py` | 111 | `<evalsets/<name>/vN> [--min*] [--out]` | #10 冻结前条数闸门 |
| `model_robust.py` | 173 | `<results目录> [--out]` | #15 跨模型 |
| `compare.py` | 151 | `--expect <f> --actual <f> [--aggregate <dir>] [--model] [--events] [--out] [--build-only]` | #14 |
| `trigger_judge.py` | 101 | `--skill <SKILL.md> --trace <trace.json> [--model] [--events] [--out] [--build-only]` | #10 触发判定 |
| `score.py` | 106 | `<results目录>` | #10/#13/#11 |
| `judge_runner.py` | 67 | `--validate <judge输出.json>` | rubric 契约门禁 |
| `idem.py` | 41 | `<trace1.json> <trace2.json>` | #16 |
| `pairwise.py` | 104 | `--prompt --a --b [--out] [--build-only]` | T2B 无标签 A/B 盲评 |
| `calibrate.py` | 207 | `--metric <name> [--cases] [--samples] [--workers]` | Judge 校准（agreement/accuracy） |
| `check-deps.sh` | — | `bash scripts/check-deps.sh`（退出码 0/1） | #7 前置自检，非 Python |

## SYMBOL MAP

顶层函数 + 行号。

- `report.py` — `worse`(44) `load`(57) `load_judge`(76) `_sha256_files`(84) `evaluator_fingerprint`(94) `skill_fingerprint`(115) `evalset_meta`(133) `build_meta`(150) `write_meta`(168) `verdict_of_ratio`(179) `verdict_of_score`(184) `from_static`(188) `cv_of`(239) `dependency_errors`(263) `build`(298) `_evalset_reviewed_by`(563) `conclusion`(583) `_esc`(604) `_flow_svg`(609) `_skill_meta`(625) `_evalset_html`(654) `_data_table`(849) `_html_table`(904) `_md_table`(912) `_raw_html`(920) `_badge`(960) `_glyph`(965) `_advice`(970) `_flatten_table`(1015) `_tables`(1033) `_overview_html`(1122) `_advice_html`(1134) `_judge_summary`(1158) `render_html`(1173) `render_md`(1227) `main`(1268)；常量 `DIM_PERM_GROUPS` `DIM_PERM_HARD`/`DIM_PERM_SOFT` `ADVICE_ACTIONS`(937) `VF_GLYPH`(932) `CONCLUSION_CLS`(1170) `_HTML_TMPL`(720)
- `trace_run.py` — `EventAggregator`(19, class；`.feed`(30) `.result`(66)) `parse_events`(88) `build_args`(101) `main`(124)
- `static_check.py` — `_luhn_ok`(170) `_cn_id_ok`(185) `_public_ip_ok`(197) `_pii_accept`(208) `_shebang_tool`(238) `_norm_tool`(264) `_stdlib_names`(274) `collect_deps`(279) `parse_frontmatter`(327) `find_stale_refs`(350) `walk_tree`(375) `scan_patterns`(392) `emit`(434) `resolve_excludes`(442) `default_excludes`(451) `rel_paths`(474) `main`(485)；常量 `DANGEROUS_PATTERNS`(78) `SECRETS_PATTERNS`(94) `PLACEHOLDER_RE`(105) `INJECTION_PATTERNS`(111) `EXFIL_PATTERNS`(126) `OBFUSCATION_PATTERNS`(140) `PII_PATTERNS`(156) `PII_VALIDATORS`(205) `HOST_HARDCODE_PATTERNS`(223) `BASELINE_DEPS`(237)
- `evalset_check.py` — `normalize`(35) `grams`(40) `coverage`(47) `load_group`(55) `find_duplicates`(75) `find_conflicts`(87) `check`(98) `main`(151)
- `model_robust.py` — `normalize`(34) `grams`(38) `jaccard`(45) `load_model_dir`(52) `mean`(71) `analyze`(75) `main`(150)
- `compare.py` — `build_prompt`(15) `extract_answer`(22) `main`(52)
- `trigger_judge.py` — `build_prompt`(17) `main`(29)
- `score.py` — `load_json`(16) `prf1`(24) `stats`(40) `necessity`(53) `main`(75)
- `judge_runner.py` — `score_of_items`(14) `validate`(20) `main`(52)
- `idem.py` — `load_trace`(12) `main`(22)
- `pairwise.py` — `build_prompt`(20) `_read`(33) `main`(40)
- `calibrate.py` — `_majority`(32) `summarize`(41) `_run_judge`(84) `main`(99)

## CONVENTIONS

- **同目录导入靠 `sys.path.insert(0, Path(__file__).parent)`**（每个 `main()` 里）。因此脚本必须以 `scripts/` 为基准被调用。
- **argv 手写解析**（`while i < len(args)`），不用 argparse。16 个脚本各写一遍——加参数时跟随既有写法。
- **stdout 契约字段恒输出**：输入缺失或非法 → 输出 `"skipped"`，**不崩溃、不打 0 分**（`score.py`、`report.py` 均如此）。
- **LLM 调用两条路径**：T1 走 `llm.py`（OpenAI-compatible/Anthropic，无 tools）；T2+ 走 `harness.py`（当前 pi CLI）。旧 `_subprocess` 形态只在 `--backend pi` 回退时用。
- **离线 seam 三件套**：`--build-only`（只打印将执行的命令）、`--events <jsonl>`（离线解析已采集事件流）、`--out <file>`。测试全靠它们，改这三个 flag 会同时打断多个测试。
- 阈值常量不再放文件头部：统一在 `config.yaml`（ADR-0035），脚本内用 `_config.get_*` 惰性读取（**不在 import 时读文件**，保持模块 import 无副作用）。

## ANTI-PATTERNS

- 不用 argparse / click 重写 argv 解析。
- 测量脚本之间不互相 subprocess 调用：串联只靠 `results/<version>/` 下的产物文件。例外是编排层 `runner.py`（调 CLI）与 `harness.py`（调 trace_run 的 pi 子进程）。
- 不改 stdout JSON 的字段名、不新增必填字段——`report.py` 按名读中间产物，改名即断链。
- 不在 `main()` 之外做 IO：模块顶层不读写文件，保证 import 无副作用（`tests/test_trace_run.py:179` 会直接 import 模块）。
- 不硬编码解释器或机器路径，一律 `sys.executable`（ADR-0009）。
- `DANGEROUS_PATTERNS` 里的 `su`+`do` 拼接（`static_check.py:85`）是合并前另一条线的写法，与 ADR-0012 的自排除**并存**；两条都保留，别只删其一（删自排除会让自检失败）。
- 不把 `SECRETS_PATTERNS` 的占位符过滤（`PLACEHOLDER_RE`，`static_check.py:105`）删掉：文档里合法写 `api_key="your-key-here"` 会被误报为硬编码凭据（ADR-0013）。PII 组（`static_check.py:156`）同样在 `_pii_accept` 里先过占位符与示例白名单再进 Luhn/证件校验位/公网判断。
- 不把 `OBFUSCATION_PATTERNS` / `PII_PATTERNS` 的裁决提到 fail：两组只能 warn，命中多为打包产物或合法示例（ADR-0013/0029，`report.py:188` 的 `from_static`）。
- 不让 `deps_missing` 把 #12 判成 fail：缺声明是文档缺陷，与 trace 面“跑得通”的证据冲突（ADR-0031，`report.py` 的 #12 第三源）。
- 优化建议必须**确定性派生**（`report.py:970 _advice`）：不得引入 LLM 生成建议——建议与裁决同源才谈得上强对应（ADR-0030）。

## NOTES

- 重复实现（抽公共模块前先读根 `AGENTS.md` 关于指纹的警告）：`normalize()` `evalset_check.py:45` vs `model_robust.py:38`（字节级相同）；`grams()` `:50` vs `:42`；`coverage()`(`:57`) 与 `jaccard()`(`model_robust.py:49`) 同源但分母不同（非对称覆盖 vs 并集）。
- 代码级 import 扇入：`compare.extract_answer` ← `trigger_judge.py`；`static_check.parse_frontmatter` ← `evalset_check.py`；`trace_run.EventAggregator` ← `tests/test_trace_run.py`。其余脚本零代码级依赖。
- `report.py:298 build()` 是约 260 行 if 链，16 指标无子函数。读它反推所有上游契约是最快路径。
- `report.py:720 _HTML_TMPL` 是约 100 行内联 HTML + 前端 JS（含评测集审核与建议 tab 的交互）。改 HTML 时注意 JS 字符串里的换行会被截断（历史 bug：commit `0ef26f5`），且指标卡必须带 `id='m-N'`——建议页的锚点靠它跳转（ADR-0030）。
- 评估器指纹只收 `scripts/*.py` + `judges/*.md` + `config.yaml` + `SKILL.md` + `reference.md`（`report.py:94`）——`check-deps.sh` 改了**不会**让历史报告变 `comparable=false`，改 `.py`/`config.yaml` 会。
- 新增 `check-deps.sh` 的坑：Windows 上 `python3` 可能是应用商店占位符，`command -v` 能找到但跑不起来，必须实跑验证再采纳（`check-deps.sh` 已这么写，别简化成 `command -v python3`）。
