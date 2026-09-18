# skill 包自闭环：scripts/tests 移入 skill-evaluator/ 内

演练（evalsets/skill-evaluator/results/7606790/report.json #8）与全仓复审一致判定：脚本在仓库根而 SKILL.md 在包内，导致按 SKILL.md:16 前置自检必然报"评估器自身损坏"，#8 最小依赖两次独立实测不达标。

决定：`scripts/` 与 `tests/` 移入 `skill-evaluator/` 包内；SKILL.md/reference.md 中所有相对路径以**被测 skill 目录（评估器自身即 skill-evaluator/）**为基准。评测集仍在评估器目录 `skill-evaluator/evalsets/`（ADR-0002），不随被测 skill 走。

## Consequences

- 评估器可整体复制/安装到 `~/.pi/agent/skills/` 后独立工作
- 测试相对路径 `Path(__file__).parent.parent / "scripts"` 移动后仍然成立，60 个测试零改动全绿
