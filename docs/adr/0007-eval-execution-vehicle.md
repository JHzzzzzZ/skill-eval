# 沙箱运行用 pi CLI 子进程逐 case 执行，触发即停，不用 subagent

触发评测与主运行需要真实跑（禁止 self-simulation）。决定：每个 case 起一个 `pi --mode json --no-session` 子进程（trace_run.py 逐 case for 循环），在事件流中检测到"已实际使用该 skill"即提前终止该次运行（early-exit），不用 agent subagent 机制做"跟踪 + kill"。

原因：pi CLI 子进程本身就隔离、单次成本已低且天然支持 `--model` 配置；subagent + kill-on-trigger 是用更复杂的机制解决已解决的问题。early-exit 只需在消费事件流时多判一步，达成同样的省 token 效果。

## Considered Options

- subagent 跟踪 + 触发后 kill：隔离更细，但引入子代理生命周期管理与最大并发数约束，复杂度不成比例 → 拒绝
- pi CLI 子进程 + 事件流 early-exit：约 20 行改动，机制与既有 trace_run.py 一致 → 采用

## Consequences

- 若未来要支持 pi 以外的 agent 跑沙箱，需要在 trace_run.py 上加 adapter 层；本期只留接口声明不实现

## 修订（2026-09-20）：early-exit 停止条件 = 首次指向被测 SKILL.md 的调用

初版实现把停止条件定为「事件流首次工具调用」。实测发现偏差：pi 是渐进式披露
（启动时仅 name/description 进上下文，SKILL.md 全文由模型决定使用时自己 read），
agent 可能先探索若干步才加载 skill（todo-add 评测 trig-shouldnot-3 实证：第 4 步才读）。
若在首次任意调用处 kill，这类触发 run 会被误杀成假阴性，系统性低估 recall。

修订：停止条件改为「首次 args 指向被测 SKILL.md 的工具调用」——渐进式披露下
这就是加载事件本身，即触发决定点；未出现该调用的 run 跑完整，其行为正是
precision 的证据。skill_marker=None 时退回旧口径（无 skill 场景/离线测试）。
