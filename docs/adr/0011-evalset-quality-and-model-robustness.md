# 触发集质量与跨模型一致性：折进 #1/#12，不新增第 20 指标

两个可信度信号需要落地：触发集是否照抄 description（ADR-0002 早已写下的规则，但一直没有执行检查），以及同一 skill 换模型跑是否行为漂移（spec #12 原话"几个不同的环境里面去跑几次"）。19 条指标是报告契约（"19 条指标全部出现"），不能加第 20 条。决定：

1. 新增 `scripts/evalset_check.py`：用确定性字符 3-gram 覆盖率查四类问题——should prompt 与 description 高度重合（≥0.6）、should prompt 直接含 skill name、组内近似重复（≥0.8）、同句同时出现在 should 与 should-not
2. 命中 `clean=false` → report.py 把 #1 降级为 `warn`（原本 `fail` 保持 `fail`），证据进 `#1.data.evalset_quality`；**不做整体闸门**（与 ADR-0008 的区别：那是客观硬事实，这里是启发式阈值，疑似不该一票否决）
3. 新增 `scripts/model_robust.py`：读 `results/<version>/models/<模型名>/trace-<序号>.json`，一致率 ≥0.8 判鲁棒；裁决折进 #12
4. #12 的两个信号在同一 data 下分写（`intra_model_cost` / `model_robust`），不合并成一个数字；跨模型**只能降级不能升级**——它不能代替组内重复运行
5. 跨模型默认不跑（成本 ×模型数），SKILL.md 列为可选步骤

原因：这两个信号影响的是"指标数字可不可信"，不是 skill 质量本身，所以让它们修改既有指标的裁决与证据，而不是新增维度；判定全部用确定性算法，不调 LLM——把可信度判断也做成需要评审的黑箱，等于多一层不可复现的怀疑。

## Considered Options

- 新增第 20/21 指标：破坏 19 条报告契约，前端指标列表与 evolution 对比都要跟着改
- 照抄检测用 LLM 语义判定：能抓改写、更准，但成本高、不可复现，且判定者与被测同源（reference.md 已记录该偏差）；先上确定性版本
- 触发集质量做整体闸门 fail：启发式阈值会误杀，见决定 2
- 跨模型默认跑：成本翻倍，违反 ADR-0009 的"默认保守"精神

## Consequences

- `#1` 会出现"F1 很高但 verdict=warn"——note 写明原因，数字仍保留在 data 里可核对
- 未跑跨模型时 #12 行为与改动前完全一致（不扣分）
- 跨模型目录的顺序必须与评测集一致（序号即 case 身份）；某模型缺该序号时该 case 不计入一致率
