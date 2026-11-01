# PROJECT KNOWLEDGE BASE — skill-evaluator

Generated: 2026-11-01（v2 更新：ADR-0032–0040）

## OVERVIEW

评估器 skill 包本体：`SKILL.md`（入口/路由）+ `reference.md`（契约全集）+ `config.yaml`（阈值）+
`scripts/`（CLI + runner + llm/harness）+ `judges/`（rubric）+ `web/`（本地前端）。
评估一个 skill 包，产出 16 条指标（ADR-0032）的 `report.json` / `report.md` / `report.html`。实现流程见根 `AGENTS.md § IMPLEMENTATION WORKFLOW`。

## WHERE TO LOOK

| 任务 | 位置 |
|---|---|
| 加/改一条指标 | `reference.md § 指标 → 测量手段映射`，再改 `scripts/report.py` 的 `DIM_NAMES`/`ALL_KEYS` |
| 改报告契约（16 key / schema 2 / skipped / 三档结论） | `scripts/report.py` 的 `build()` / `conclusion()`；契约锁在 `tests/test_report.py` |
| 改报告呈现（表格化/折叠/建议/✓✗） | ADR-0030 + `report.py` 的 `_tables()` / `_advice()` / `_HTML_TMPL` |
| 改 rubric | `judges/<metric>.md`（输出节必须保持统一 JSON 契约） |
| 改评测集结构/冻结 | `reference.md § 评测集冻结` + ADR-0002 |
| 改沙箱/执行载体 | `reference.md § 沙箱` / `§ 执行载体` + `scripts/harness.py`（ADR-0034） |
| 改中间产物名 | `reference.md § 中间产物契约`（下游按名读，改名即断链） |
| 改版本号/指纹 | ADR-0003/0010 + `report.py` 的指纹+meta 层 |
| 改档位/增量补跑 | ADR-0033 + `SKILL.md` + `report.py` 的 `tier` |
| 改多源规则（主源/辅证） | ADR-0017/0031 + `report.py` 的 #2/#12/#4 合成处 |
| 改稳定性口径 | ADR-0016 + `score.py`（`cost_by_case`）+ `report.py`（`cv_of`） |
| 改扫描口径/六组规则 | ADR-0012/0029 + `static_check.py` |
| 改依赖全面性 | ADR-0031 + `static_check.py::collect_deps` |
| 改阈值 | `config.yaml`（ADR-0035）；解析器 `scripts/_config.py` |
| 改 UI / 进度 | `web/app.py` + `web/index.html` + `runner.py` 的 NDJSON 进度（ADR-0036）；工作区见 0039，真实输入多 case 见 0040 |

## PIPELINE

runner 按档位增量执行（ADR-0033）：

- **T0**：`static_check.py` → `static.json`（#1/#2/#3/#4/#12 的静态面）
- **T1**：`judge_run.py`（默认 `llm`，可 `--backend pi`）→ `judges/<metric>.json`（#2/#5/#6/#7/#8/#9，`contract` 一份服务 #8/#9）
- **T2A**：`trace_run.py --early-exit` + `trigger_judge.py` → `triggers.json` → `score.py`（#10）；human 型跳过
- **T2B**：1–20 条真实输入逐条跑 golden + baseline → `t2b/score.json`（#11/#12）；有 expect 算匹配率，无 expect 双向 A/B 偏好；n<3 最高 warn
- **T3**：cases golden ×N + `idem.py` + `compare.py` → `runs.json`/`idem.json`/`compare.json`（#13/#14/#15/#16）
- **report**：`report.py --tier <t> --html` → `report.json`（schema 2 + legacy_id_map）+ md/html/meta

前端 `web/app.py` 用 stdlib HTTP + SSE 调 runner，只绑 `127.0.0.1`。

## JUDGES

| 文件 | 指标 | 评什么 |
|---|---|---|
| `brevity.md` | #2 语义面 | 无下沉细节、有索引（行数由 `static_check.py` 测） |
| `redundancy.md` | #5 | 无概念科普 / 复读 frontmatter / 与附属文件重复 / 客套 |
| `fallback.md` | #6 | 失败有重试/降级/终止，依赖缺失行为明确，失败可见 |
| `precheck.md` | #7 | 环境校验置前且覆盖完整 |
| `contract.md` | #8/#9 | 输入声明、输出路径、输出格式稳定（一份 rubric 服务两个指标） |
| `deps.md` | #12 语义面 | 依赖已声明、无未打包外部环境依赖、凭据走 fallback、声明清单完整 |
| `pii.md` | #4 语义面 | 无真实个人标识、无公司内部标识、示例数据全为占位、无未脱敏日志样本 |
| `trigger.md` | #10 | 触发判定：是否加载被测 skill（`trigger_judge.py`） |

`deps.md` / `pii.md` 不在 `report.py` 的 judges 映射表里：#12 三源合成（trace 报错优先 + `judges/deps.json` + `deps_missing`），#4 = 静态六组 + 闸门与 `judges/pii.json` 取较差者。统一 JSON：

```json
{"items":[{"name":str,"pass":bool,"quote":str}], "score":0~1, "evidence":[str], "reason":str}
```

`score` = pass 项数/总项数；`score<1` 时 `evidence` 必须非空。

## EVALSET CONTRACT

`evalsets/<name>/v<N>/`（评估器目录，不放被测 skill）：

- `triggers/should/*.json`、`triggers/should-not/*.json`、`triggers/confusable/*.json` — `{"prompt": str}`
- `cases/*.json` — `{"prompt": str, "expect": str}`
- `meta.json` — `{"frozen_at", "reviewed_by": "human"|"ai"}`；没有它就是未冻结

## 包内禁令

- 评测集位置固定 `evalsets/<name>/v<N>/`，不放被测 skill（ADR-0002）。
- 写 `meta.json` 不静默覆盖人工键（ADR-0010）。
- 改 `scripts/`、`judges/`、`config.yaml`、`SKILL.md`、`reference.md` 会让评估器指纹变，历史报告 `comparable=false`。
- 沙箱失败不静默丢指标：降级并在报告标注原因。
- 不改 #4 的命中分级：`dangerous`/`secrets`/`injection`/`exfil` 命中即 fail，`obfuscation`/`pii` 只 warn（ADR-0013/0029）。
- 不让 `deps_missing` 把 #12 判成 fail（ADR-0031）。
- 优化建议必须从裁决确定性派生，不得 LLM 生成（ADR-0030）。
- T4 的 `attic/` 内容不随包分发、不跑测试（ADR-0032）。
- 前端不重算指标，只调 runner；报告契约以 `report.py` 为准（ADR-0036）。
