# 沙箱运行用 pi CLI 子进程逐 case 执行，触发即停，不用 subagent

触发评测与主运行需要真实跑（禁止 self-simulation）。决定：每个 case 起一个 `pi --mode json --no-session` 子进程（trace_run.py 逐 case for 循环），在事件流中检测到"已实际使用该 skill"即提前终止该次运行（early-exit），不用 agent subagent 机制做"跟踪 + kill"。

原因：pi CLI 子进程本身就隔离、单次成本已低且天然支持 `--model` 配置；subagent + kill-on-trigger 是用更复杂的机制解决已解决的问题。early-exit 只需在消费事件流时多判一步，达成同样的省 token 效果。

## Considered Options

- subagent 跟踪 + 触发后 kill：隔离更细，但引入子代理生命周期管理与最大并发数约束，复杂度不成比例 → 拒绝
- pi CLI 子进程 + 事件流 early-exit：约 20 行改动，机制与既有 trace_run.py 一致 → 采用

## Consequences

- 若未来要支持 pi 以外的 agent 跑沙箱，需要在 trace_run.py 上加 adapter 层；本期只留接口声明不实现
