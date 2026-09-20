# 可移植性闸门：只查宿主环境硬编码，不查"声明给某 agent 用"

skill-evaluator 需要一个通用性闸门，但其边界曾有两种理解：拦"针对特定 agent 的 skill"，或拦"硬编码宿主环境"。决定采用后者：static_check 新增编号检查项——被测 skill 中出现宿主环境硬编码（绝对路径、特定用户目录、.claude/.cursor 等他方生态路径）→ **fail**。

#7 调用方式的三种 frontmatter 模式（默认 / disable-model-invocation / user-invocable）只决定"谁能调用"，与评估通用性无关，不构成 fail 场景。真给其他 agent 生态写的 skill 会被硬编码闸门自动捕获，无需单独规则。

原因：评估器自身是 pi 专属（沙箱跑 pi CLI），要求被测 skill 与 agent 无关自相矛盾；但写死某台机器的路径（如特定用户的 .venv 绝对路径）在任何宿主上都不可移植，这才能作为整体 fail 的客观标准。

## Consequences

- 闸门是客观的字符串扫描，可复现、无需 LLM
- skill 合法引用自身相对路径或环境变量不算硬编码，扫描需排除这两类
