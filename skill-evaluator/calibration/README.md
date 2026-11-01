# Judge 校准集

用途：测 LLM 评审的**逐项一致率**和**相对人工标注的准确率**，不是测被测 skill 本身。

## 目录格式

```
calibration/<metric>/<case>/
├── skill/              # 被测 skill 的最小包（SKILL.md + 可选 reference/scripts）
└── expected.json       # {"items": {"检查点名": true/false}}
```

`expected.json` 放在 `skill/` 外层，避免被 `judge_run.collect_files` 当评审材料发给模型。

## 跑法

```bash
cd skill-evaluator
python scripts/calibrate.py --metric brevity --samples 3
python scripts/calibrate.py --metric precheck --samples 3
```

需要真实 LLM 配置（页面/`config.yaml`/环境变量，同 T1）。结果默认写
`calibration/results/<metric>.json`（gitignore）。

## 判定

- `agreement`：同一 case 多次评审，逐项多数票的一致比例
- `accuracy`：多数票是否等于 `expected.json`
- 任一项 `agreement < --min-agreement`（默认 0.8）或 `accuracy < --min-accuracy`（默认 0.9）→ 退出码 1
- 当前是种子集（brevity 8 例、precheck 20 例），目标扩到每项 20–30 例：10 个明确 pass、10 个明确 fail、5 个边界
- 种子基线：brevity 8 cases × 3 = agreement 0.98 / accuracy 1.00；precheck 8 cases × 3 = 0.98 / 1.00。precheck 扩到 20 例后待重跑。样本少且是人工构造，**只用于回归，不作为上线结论**。
- 单发对照：同一批 precheck 两次 1 sample，`声明与实现一致` 分别得到 0.75 和 1.00；3 samples 多数票为 1.00。单发评审波动明显，不能当结论，这也是默认 `--samples 3` 的原因。
- 20 例 × 3 首次结果：overall agreement 0.98，accuracy 0.93；`声明与实现一致` agreement 0.92 但 accuracy 0.60。逐 case 明细显示 8/20 是**标注/定义不一致**（无承诺被标成不一致、脚本多查被标成不一致、正文承诺停止但脚本静默通过被标成一致）。已按 `judges/precheck.md` 的操作定义修正标注并重写该检查点；这轮数据不当作通过结论。
- 20 例 × 3 重跑（并发 4）：overall agreement 0.99，accuracy 0.99；仅 `p10-required-optional` 的「声明与实现一致」不一致。逐条看是 rubric 把“分级”也塞进第 4 项，和“分级正确”重复扣；已把分级差异明确归第 6 项。
- v3：overall 0.98 / 0.99；`p13-silent-fail` 的“分级正确”标注修正为 false，另有 1 次 `LLM 返回空文本`。`judge_run` 已加空响应重试；待重跑。
- v4：overall 0.99 / 0.99，仍因 p18 一次空响应 `passed=false`；p13「失败处置明确」模型判 true，但脚本 `[FAIL]` 后 `exit 0` 按 rubric 应为 false。已把“打印 FAIL 但 exit 0 不算处置”写进 rubric，并给 calibration 加 `--retries`（默认 1）。

## 注意

- 不要用校准集反向调 rubric 到“全过”；先看 disagreement 的具体检查点，再改定义。
- 校准集不参与评估器指纹，改标注不会让历史报告变 `comparable=false`。
- 不提交 `calibration/results/`。
