---
name: p16
description: 自检放在流程末尾
dependencies: python3, git
---

# 流程

1. 读取输入。
2. 执行评估。
3. 运行 `bash scripts/check-deps.sh`；失败就停止。
