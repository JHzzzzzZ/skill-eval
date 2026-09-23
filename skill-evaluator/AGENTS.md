# PROJECT KNOWLEDGE BASE — skill-evaluator

Generated: 2026-09-22T13:43:08.578Z

## OVERVIEW

评估器 skill 包本体：`SKILL.md`（编排，只放立即要做的事）+ `reference.md`（契约全集）。评估一个 skill 包，产出 19 条指标。

## WHERE TO LOOK

| 任务 | 位置 |
|---|---|
| 加/改一条指标 | `reference.md § 指标 → 测量手段映射` 先定位测量手段，再改对应脚本 + `scripts/report.py:20 DIM_NAMES` |
| 改报告契约（19 key / skipped / 三档结论） | `scripts/report.py:207 build()` + `:336 conclusion()`；契约锁在 `tests/test_report.py` |
| 改 LLM 评审 rubric | `judges/<metric>.md`（输出节必须保持统一 JSON 契约） |
| 改评测集结构或冻结流程 | `reference.md § 评测集冻结` + `docs/adr/0002` |
| 改沙箱 / 执行载体 | `reference.md § 沙箱` + `§ 执行载体`（`docs/adr/0001`/`0007`） |
| 改中间产物文件名 | `reference.md § 中间产物契约`（下游脚本按名读，改名即断链） |
| 改版本号 / 指纹 | `docs/adr/0003`、`docs/adr/0010` + `scripts/report.py:67-160`（指纹+meta 层） |
| 改档位定义 / 增量补跑 | `docs/adr/0014` + `SKILL.md` 档位表 + `scripts/report.py` 的 `tier` 字段 |
| 改 #12 稳定性口径 | `docs/adr/0016` + `scripts/score.py`（`cost_by_case`）+ `scripts/report.py`（`cv_of`） |
| 改扫描口径 / 五组规则表 | `docs/adr/0012`（口径）、`docs/adr/0013`（五组分级）、`docs/adr/0015`（作用域）+ `scripts/static_check.py:45-123` |
| 改 fallback 行为 | `reference.md § Fallback` 表 |

## PIPELINE

SKILL.md 的 9 步，按五档（T0–T4，默认 T2）增量执行（ADR-0014）。脚本之间**没有 subprocess 调用**，串联只靠 `evalsets/<name>/results/<version>/` 下的产物文件：

1. 前置自检（`bash scripts/check-deps.sh` → python≥3.9/git/pytest/pi + 包内文件齐备；再确认被测有 `SKILL.md`）
2. 存档定版（原始上传只读存档 `uploads/`；副本 `git init` → 版本号）
3. `static_check.py` → `static.json`（#2/#7 + #13 五组规则 + 可移植性闸门）
4. 评测集冻结；冻结前先 `evalset_count.py` 卡条数（三组各 ≥10），再 `evalset_check.py` → `evalset.json`（#1 可信度），通过后 LLM 自动审核（`reviewed_by: "ai"`）即冻结
5. 沙箱运行 `trace_run.py`（触发 / golden / baseline / 重复 ×3 / 可选跨模型）→ `triggers.json`、`runs.json`、`golden.json`、`baseline.json`
6. LLM 评审：按 `judges/*.md` 产出 → `judge_runner.py --validate` 通过 → `judges/<metric>.json`
7. `score.py` → `score.json`；`idem.py` → `idem.json`
8. `compare.py` → `compare.json`（#9）、`process.py` → `process.json`（#10），再 `report.py` → `report.json` + `report.md` + `meta.json`

## JUDGES

| 文件 | 指标 | 评什么 |
|---|---|---|
| `brevity.md` | #3 | SKILL.md ≤150 行、无下沉细节、有索引 |
| `redundancy.md` | #6 | 无概念科普 / 复读 frontmatter / 与附属文件重复 / 客套 |
| `fallback.md` | #11 | 失败有重试/降级/终止，依赖缺失行为明确，失败可见 |
| `precheck.md` | #16 | 环境校验置前且覆盖完整 |
| `contract.md` | #17/#18 | 输入声明、输出路径、输出格式稳定（一份 rubric 服务两个指标） |
| `side-effects.md` | #19 | 无未防护不可逆操作、有 dry-run / 确认 |
| `deps.md` | #8 语义面 | 依赖已声明、无未打包外部环境依赖、凭据走 fallback |

`deps.md` **不在** `report.py` 的 judges 映射表里——#8 是双源合成：trace 报错（实跑面）优先，无报错才用 `judges/deps.json`（语义面），见 `scripts/report.py:232-243`。

统一输出契约（`judge_runner.py` 强制；7 份 rubric 都写着同一句"不要自行发挥"）：

```json
{"items":[{"name":str,"pass":bool,"quote":str}], "score":0~1, "evidence":[str], "reason":str}
```

`score` 必须 = pass 项数/总项数（两位小数）；`score<1` 时 `evidence` 必须非空并引用原文。

## EVALSET CONTRACT

`evalsets/<name>/v<N>/`（在**评估器目录**，不放被测 skill）：

- `triggers/should/*.json`、`triggers/should-not/*.json`、`triggers/confusable/*.json` — 仅需 `{"prompt": str}`
- `cases/*.json` — 需 `{"prompt": str, "expect": str}`
- `meta.json` — `{"frozen_at", "reviewed_by": "human"}`；没有它就等于未冻结

`DRAFT-README.json`（内容 `{"note": "draft - NOT frozen, pending human review"}`）是草稿标记，即未冻结。

包内操作专属禁令。跨目录生效的禁令见根 `AGENTS.md`，此处不重复。

- 评测集位置固定 `evalsets/<name>/v<N>/`（评估器目录内），**不放被测 skill 目录**（ADR-0002）——污染被测对象。
- 写 `meta.json` 不静默覆盖人工键：评估器只用 `evaluator`/`skill_fingerprint`/`evalset_source`，避开人工已用的 `skill`/`evalset`（ADR-0010）。
- 改完 `scripts/`、`judges/`、`SKILL.md`、`reference.md` 后**不得**把历史报告差异当回归证据——这四类是评估器指纹范围，一改指纹就变，`evolution.py` 会标 `comparable=false`（ADR-0010）。
- 评估器目录内不放针对特定被测 skill 的脚本；历史证据存 `evalsets/<name>/provenance/`（`reference.md:82`）。
- 沙箱运行失败不静默丢指标：降级为"静态 + LLM 评审"并在报告标注原因（`SKILL.md:31`）。
- 沙箱定义只写 `reference.md`，不在 `SKILL.md` 内联（ADR-0001）——否则换 Docker 要改流程逻辑。
- #8 不只读 `judges/deps.json`：trace 报错（实跑面）优先，无报错才用语义面（`scripts/report.py:232-243`）。
- 不改 #13 的命中分级：`dangerous`/`secrets`/`injection`/`exfil` 命中即 fail，只有 `obfuscation` 是 warn（待人工复核）——混淆多为打包/压缩产物，提成 fail 会误杀（ADR-0013，`report.py:186-203`）。
- 不删 `secrets` 组的占位符过滤（`PLACEHOLDER_RE`）：文档里合法写 `api_key="your-key-here"` 不该被当硬编码凭据。
