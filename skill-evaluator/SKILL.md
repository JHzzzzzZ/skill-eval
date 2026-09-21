---
name: skill-evaluator
description: Evaluate a skill package against 19 quality metrics, producing report.json and report.md. Use when the user explicitly asks to evaluate, assess, or 评估 a skill package (give it the skill path).
---

# Skill Evaluator

评估一个 skill 包的质量，产出 19 条指标的报告。完整流程细则、指标定义、rubric 索引见 [reference.md](./reference.md)——本文件只放立即要做的事。

## 输入

用户给出被测 skill 的路径。缺路径时先问，不要猜。

本文件与 `scripts/`、`judges/`、`reference.md` 同目录，所有相对路径以本 skill 目录为基准（ADR-0006 自闭环）。

## 流程

入口参数：`--tier <static|review|trigger|core|full>`（或 T0–T4），**默认 T2 (trigger)**。档位是增量关系：高档复用低档产物（按被测 skill 的 content_sha256 判定，不一致则作废重跑）。低档先行、评测集就绪后从缺失档补跑，称**增量补跑（staged）**；沙箱运行失败退回低档才称**降级（fallback）**。

| 档 | 名称 | 做什么 | 覆盖指标 | 预估耗时 |
|---|---|---|---|---|
| T0 | static | 前置自检 + 存档定版 + 静态检查 | #2 #7 #13 | ~1s |
| T1 | review | T0 + 7 项 LLM 评审 | + #3 #6(评审面) #11 #16 #17 #18 #19 | ~3min |
| T2 | trigger | T1 + 触发评测（--early-exit，默认档） | + #1 | ~10min |
| T3 | core | T2 + golden 主运行 + 基线 A/B + 重复×N + 幂等 + compare | + #4 #5 #8 #9 #12 #15 | ~25min |
| T4 | full | T3 + process 审计 + 消融 + evolution | + #6(实证) #10 #14 | ~1h |

T0/T1 不依赖评测集，#1/#5/#9 标 skipped；评测集就绪后增量补，不重跑 T0/T1。

1. **前置自检**：确认 `scripts/`、`judges/`、`reference.md` 存在；被测路径下有 `SKILL.md`。任一缺失 → 走 [reference.md § Fallback](./reference.md)
2. **存档与定版**：原始上传只读存档到 `uploads/<name>-<时间戳>/`；副本上 `git init + commit`。版本号规则见 [reference.md § 版本号](./reference.md)
3. **静态检查**（T0+）：运行 `scripts/static_check.py <skill目录>`，覆盖 #2/#7/#13
4. **评测集**（T2+ 前置）：检查 `evalsets/<name>/` 是否已冻结；无则走 [reference.md § 评测集冻结](./reference.md)：自动生成 → 条数闸门 → **LLM 自动审核**（用与生成不同的模型，见 § 评测集 AI 审核）→ 通过即冻结（`reviewed_by: "ai"`），不阻塞人工
5. **触发评测**（T2+）：三组触发 prompt 带 `--early-exit` 跑触发评测，`trigger_judge.py` 判定，服务 #1
6. **核心沙箱**（T3+）：golden 主运行 + 基线 A/B + 重复×N + idem + compare，服务 #4/#5/#8/#9/#12/#15
7. **LLM 评审**（T1+）：逐项按 `judges/` rubric 产出 JSON，`judge_runner.py --validate` 通过后存 `judges/<metric>.json`
8. **全量实证**（T4+）：process.py 审计 + ablation.py 消融 + evolution.py 版本对比
9. **计算与报告**：`scripts/score.py` + `scripts/idem.py`（T3+）→ `scripts/report.py`（#9 用 `scripts/compare.py`，T3+）。report.json 记录 `tier` 字段

## 边界

- 用户没提供评测集且不想等生成 → 第 4 步标 `skipped`，#1/#5/#9 不打分
- 沙箱运行失败 → 降级（fallback）到最近一个已完成的低档继续，报告标注降级原因与缺失指标，不要静默丢指标
- #10 过程审计：golden trace 就绪后用 `scripts/process.py`，裁决落 `process.json` 后重跑 report.py
- #14 版本演进：同 skill 有多个版本报告时用 `scripts/evolution.py evalsets/<name>` 输出变化表

## 完成后

报告路径告诉用户。被测 skill 是 git 仓库时，把评估产物 commit 到当前分支。
