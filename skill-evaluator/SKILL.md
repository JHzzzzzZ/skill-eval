---
name: skill-evaluator
description: Evaluate a skill package against 19 quality metrics. Only runs when explicitly asked (e.g. "评估这个 skill"), never auto-triggers.
disable-model-invocation: true
---

# Skill Evaluator

评估一个 skill 包的质量，产出 19 条指标的报告。完整流程细则、指标定义、rubric 索引见 [reference.md](./reference.md)——本文件只放立即要做的事。

## 输入

用户给出被测 skill 的路径。缺路径时先问，不要猜。

本文件与 `scripts/`、`judges/`、`reference.md` 同目录，所有相对路径以本 skill 目录为基准（ADR-0006 自闭环）。

## 流程

1. **前置自检**：确认 `scripts/`、`judges/`、`reference.md` 存在；被测路径下有 `SKILL.md`。任一缺失 → 走 [reference.md § Fallback](./reference.md)
2. **存档与定版**：原始上传只读存档到 `uploads/<name>-<时间戳>/`；副本上 `git init + commit`。版本号规则见 [reference.md § 版本号](./reference.md)
3. **静态检查**：运行 `scripts/static_check.py <skill目录>`，覆盖 #2/#7/#13
4. **评测集**：检查 `evalsets/<name>/` 是否已冻结；无则走 [reference.md § 评测集冻结](./reference.md)（自动生成 → 交人工审核，审核通过前不得进入第 5 步）
5. **沙箱运行**：开 git worktree 作为沙箱（沙箱定义见 [reference.md § 沙箱](./reference.md)），跑触发评测 + 主运行 + 基线 A/B + 重复运行 ×N，采集 trace 与中间产物（契约见 [reference.md § 中间产物](./reference.md)），一次运行同时服务 #1/#4/#5/#8/#9/#12/#15
6. **LLM 评审**：逐项按 `judges/` rubric 产出 JSON，`judge_runner.py --validate` 通过后存为 `judges/<metric>.json`，覆盖 #3/#6/#11/#16/#17/#18/#19
7. **计算**：`scripts/score.py <results目录>`（P/R/F1、成本、必要性）+ `scripts/idem.py`（幂等）
8. **报告**：`scripts/report.py <results目录>` 产出 report.json + report.md（输入中间产物见 [reference.md § 中间产物](./reference.md)）；#9 用 `scripts/compare.py` 对比 expect/actual，裁决落 `compare.json` 后再跑 report.py

## 边界

- 用户没上传评测集且不想等人工审核 → 第 4 步标 `skipped`，#1/#5/#9 不打分
- 沙箱运行失败 → 降级为"静态 + LLM 评审"，报告标注降级原因，不要静默丢指标
- #10 过程审计：golden trace 就绪后用 `scripts/process.py`，裁决落 `process.json` 后重跑 report.py
- #14 版本演进：同 skill 有多个版本报告时用 `scripts/evolution.py evalsets/<name>` 输出变化表

## 完成后

报告路径告诉用户。被测 skill 是 git 仓库时，把评估产物 commit 到当前分支。
