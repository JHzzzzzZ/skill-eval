# runner 成为单一编排，SKILL.md 只做路由

需求 4 要“选选项 → 跑 → 看进度”的前端，但仓库没有确定性 orchestrator：9 步流程写在 `SKILL.md`，
由 agent 读着执行；`report.html` 只是结果页。若前端另起一套编排，agent 路径与前端路径会产出
两套契约（19 条 vs 16 条），报告无法比较。

决定：

1. 新增 `scripts/runner.py`：把流程固化为可调用的阶段函数/子进程编排，按 `--tier` 真正短路；
   输出 NDJSON 进度事件（stage/case/percent/message）。
2. 新增 `web/`：Python stdlib `ThreadingHTTPServer` + 原生 JS，零 Node 依赖；SSE 推进度；
   只绑 `127.0.0.1`；选项 = 档位 / 执行载体 / 模型 / 评测集 / 真实输入。
3. `SKILL.md` 改成“调用 runner”的路由，不再内联每一步细节；agent 与前端共用同一套 runner
   与报告契约。
4. 前端只读被测 skill：上传 → 存档 → 副本 → 运行；不修改被测包，也不在前端重算指标。
5. `report.html` 保留为结果视图；前端是运行入口，不替代报告。

原因：为什么不继续让 agent 编排——无法给前端稳定进度，也无法在 CI 复跑；为什么不让前端拉起
pi 执行 `SKILL.md`——那还是 agent 编排，T1 解耦与 Code Agent 选择都落空；为什么 stdlib + 原生 JS——
维持零第三方依赖。代价：runner 成为新的指纹范围与测试面；`SKILL.md` 必须改，不能再声称
“评估器本体零改动”。

## Considered Options

- 前端只包一层脚本按钮：实现快，但没有自动编排与进度。
- 前端拉起 pi 执行 `SKILL.md`：保留 agent 路径，但 T1 仍依赖 pi、Code Agent 无法选。

## Consequences

- `SKILL.md` 从“9 步操作手册”变成“入口 + 路由”；流程变更只改 runner，不同步两份。
- runner 的 stdout 是 NDJSON 进度契约，前端与 CLI 共用；不能用裸 `print` 混入。
- 前端不参与指标计算，避免出现“页面口径”与“报告口径”两套数字。
