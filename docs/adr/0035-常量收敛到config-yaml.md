# 常量收敛到 config.yaml，保留既有环境变量

阈值散落在 16 个脚本头部，改一个 384/1024 要动代码、测试和文档；用户要求常量抽到 YAML、
注释要全、现有环境变量继续可用，且不新增环境变量。

决定：

1. 新增 `skill-evaluator/config.yaml`，按 `static` / `evalset` / `report` / `run` / `idem` /
   `evalset_check` / `model_robust` / `trigger_judge` / `llm` / `harness` / `ui` 分节；
   每个键注释“含义 / 单位 / 默认值 / 影响”。
2. 解析器用 stdlib 手写子集：两级键、标量、行内 `#` 注释、行内 `[a, b]`；不支持锚点、多行块、
   嵌套列表。缺文件 → 代码默认值；文件格式错 → 启动即报错并给行号，不静默降级。
3. 优先级：CLI 参数 > 现有环境变量 > `config.yaml` > 代码默认值。既有
   `SKILL_EVAL_MODEL`、`SKILL_EVAL_REVIEW_MODEL`、`SKILL_EVAL_TRIGGER_MIN` 名称与语义不变；
   不新增环境变量。
4. `config.yaml` 进评估器指纹（ADR-0010）；改阈值会让历史报告 `comparable=false`。
5. 模型/端点等运行期配置也进 config；密钥只写“从哪个已存在变量读”，不写值。

原因：为什么不用 PyYAML——运行时零第三方依赖是硬约束，装 PyYAML 会让 `check-deps.sh` 多一项
硬依赖；为什么不用 JSON/TOML——用户明确要 YAML，且注释是这次的主要诉求；为什么配置进指纹——
阈值直接改裁决，若不进指纹，两份阈值不同的报告会被误判为同一评估器。代价：手写解析器只支持
受控子集，写错格式要在启动时暴露；config 变更会让历史报告不可比。

## Considered Options

- 引入 PyYAML：解析最省事，但破坏“运行时零第三方依赖”。
- 常量继续留在代码里、只加注释：不需要解析器，但不满足“抽到 YAML”。

## Consequences

- 新增 `scripts/config.py` 与 `tests/test_config.py`；所有读阈值的脚本改成 `config.get(...)`。
- 配置错误必须在启动时报错，不能静默用默认值——否则报告会用错阈值且无人发现。
- `config.yaml` 只放可调常量，不放正则规则体；规则体仍属于代码。
