# 沙箱用 git worktree，定义写在 reference.md 可替换

评估被测 skill 需要隔离环境。决定：用 git worktree 作为沙箱，"沙箱"的精确定义只写在 `skill-evaluator/reference.md`，SKILL.md 只引用不内联。

原因：worktree 零额外搭建成本（评估器本来就要处理 git），且未来可整体替换为 Docker 而不改动 SKILL.md 的流程逻辑。#13 权限最小化在 worktree 方案下只能静态检查 + trace 审计，做不到强制拦截——接受此限制。

## Considered Options

- pi subagent（fresh context）：文件系统仍共享，隔离弱于 worktree
- Docker：隔离最彻底，但依赖 Docker Desktop 在目标机器上可用
- 临时目录 + 环境变量隔离：最轻，隔离最弱
