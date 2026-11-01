---
name: skill-evaluator
description: Evaluate a skill package against 16 quality metrics, producing report.json and report.md. Use when the user explicitly asks to evaluate, assess, or 评估 a skill package (give it the skill path); never auto-triggers.
disable-model-invocation: true
version: 2.0.0
license: MIT
compatibility: "需要 python3 >= 3.9（仅标准库）与 git；可调阈值在 config.yaml（ADR-0035）；T2+ 沙箱需 pi CLI；T1 直连 LLM 需 `llm.base_url` 与模型（`llm.model` / SKILL_EVAL_MODEL）；测试需 pytest。已实测 Windows(Git Bash) + Python 3.12。"
---

# Skill Evaluator

评估一个 skill 包的质量，产出 16 条指标的报告（编号见 ADR-0032）。完整流程细则、指标定义、rubric 索引见 [reference.md](./reference.md)——本文件只放立即要做的事。

## 输入

用户给出被测 skill 的路径；外部人员可把 skill 打成 zip 从 `web/app.py` 上传，服务端安全解压后返回路径。缺路径时先问，不要猜。

本文件与 `scripts/`、`judges/`、`config.yaml`、`reference.md` 同目录，所有相对路径以本 skill 目录为基准（ADR-0006 自闭环）。阈值改 `config.yaml`，不要改脚本常量。

## 一键运行（推荐）

```bash
python scripts/runner.py --skill <被测skill目录> --tier t2a [--evalset <vN>] [--real-input <file>]
python web/app.py            # 打开 http://127.0.0.1:8765，选档位/载体/模型，SSE 看进度
```

runner 是确定性编排，按档位短路；下面的 9 步是它内部做的事，手工排障时按步执行。

## 流程

1. **前置自检**：跑 `bash scripts/check-deps.sh`（python/git/pytest/pi + `config.yaml` 可解析 + 包内文件齐备，缺硬依赖不要继续）；再确认被测路径下有 `SKILL.md`。任一缺失 → 走 [reference.md § Fallback](./reference.md)
2. **工作区与定版**：Web 上传按内容 hash 落到 `web/workspaces/<slug>-<hash8>/skill/`，同内容复用；runner 再把该目录复制到 `results/sandbox`，在副本上 `git init` + `git config core.autocrlf false` + `commit`。版本号规则见 [reference.md § 版本号](./reference.md)
3. **静态检查**（T0）：运行 `scripts/static_check.py <skill目录>`，覆盖 #1/#3/#4（六组规则 + 出站网络 + 可移植性闸门）、#2 正文行数（主源：150 行警告 / 500 行 fail）与 #12 的依赖声明面；#1 的 description 按字符数（384 警告 / 1024 fail，ADR-0037）
4. **评测集**（T2A/T3 前置）：检查 `evalsets/<name>/` 是否已冻结；无则走 [reference.md § 评测集冻结](./reference.md)：可用 `scripts/evalset_gen.py --skill <dir> --out <vN> --count 10` 生成草稿（或前端「生成草稿」）→ **条数闸门**（`scripts/evalset_count.py <evalsets/<name>/vN>`，三组各 ≥10）→ **触发集质量自检**（`scripts/evalset_check.py <evalset目录> --skill <skill目录>`：照抄/重复/冲突先改写）→ **LLM 自动审核**（用与生成不同的模型，见 § 评测集 AI 审核）→ 通过即冻结（`reviewed_by: "ai"`），不阻塞人工
5. **触发评测**（T2A，仅 `#3 resolved=both`）：三组触发 prompt 带 `--early-exit` 跑触发评测，`trigger_judge.py` 判定，服务 #10。单 case 受 `run.trigger_timeout_s`（默认 90s）/`run.trigger_max_steps`（默认 30）/`run.trigger_max_tokens`（默认 120000）约束；触限杀进程树并跳过该 case，不记未触发。`early_exit=true` 直接判触发，不再调 LLM；只有未早停的 case 才走 `trigger_judge.py` 兜底。`disable-model-invocation: true`（`resolved=human`）的 skill 直接跳过并记 #10 `skipped`——载体测不到“按 description 触发”（ADR-0018）
6. **真实场景 A/B**（T2B）：用户提供 1–20 条真实输入（动态表单或 `.jsonl`；只有 `prompt` 必填，`note`/`expect` 选填，后端自动编号）；逐条跑 golden（加载 skill）与 baseline（不加载）两臂，服务 #11 必要性、#12 最小依赖。有 `expect` 用 `compare.py` 算匹配率；无 `expect` 用双向 A/B 盲评算偏好胜率，不叫 accuracy。未提供真实输入 → 整档 skipped，不自动生成“真实”输入（ADR-0040）
7. **核心沙箱**（T3）：`results/sandbox` 隔离副本（git 仓库）内 golden 主运行 + 重复×N + idem + compare，服务 #13 成本 / #14 结果可验证 / #15 稳定性 / #16 幂等；不建 worktree、不做每 run clean，并带 `--guard-repo <评估器仓库>` 把越界写入记进 trace（ADR-0027）；可选跨模型：换 `--model` 再跑一遍，trace 落 `results/<version>/models/<模型名>/`，`scripts/model_robust.py` 出一致率折进 #15（默认不跑，成本 ×模型数）
8. **LLM 评审**（T1）：`scripts/judge_run.py --metric <name> --skill <dir> --out judges/<metric>.json` 默认走 `llm` 接口层（固定 prompt = rubric + 被测文件，单发、无 tools，ADR-0022/0034）；`--backend pi` 可回退旧路径。原始 request/response 存 `raw/llm-judge/<metric>.json`（#2 只判两条语义检查点——行数由第 3 步的静态面负责，ADR-0017）
9. **计算与报告**：`scripts/score.py` + `scripts/idem.py`（T3）→ `scripts/report.py <results目录> --skill <被测skill目录>`（#14 用 `scripts/compare.py`，T3）产出 report.json（`schema: 2` + `legacy_id_map` + 16 key）+ report.md + meta.json（评估器指纹 / 被测 skill 指纹 / 评测集来源）。report.json 记录 `tier` / `tier_label` / `metric_tiers`；人工调整写 `overrides.json`，报告带 `manual_overrides` 与审计日志

## 边界

- 用户没提供评测集且不想等生成 → 第 4 步标 `skipped`，依赖评测集的指标不打分
- 沙箱运行失败 → 降级（fallback）到最近一个已完成的低档继续，报告标注降级原因与缺失指标，不要静默丢指标
- T4 暂不开放：`attic/scripts/` 下的 process/ablation/evolution 与 `attic/judges/side-effects.md`，不跑
- #15 跨模型：默认不跑；未跑则 #15 只有组内信号（不扣分），跑了一致率低只降级不升级
- 阈值只改 `config.yaml`；它参与评估器指纹，改阈值会让历史报告 `comparable=false`

## 完成后

报告路径告诉用户。被测 skill 是 git 仓库时，把评估产物 commit 到当前分支。
