# human 型 skill 的 T3 载体：/skill:name 展开

ADR-0018 决定 human 型（`disable-model-invocation: true`）skill 不跑 T2 触发评测，但 T3 的 golden 臂
仍要"加载 skill"——而 pi 对这类 skill 只登记不注入：`--skill <目录>` 后模型答"无 available_skills 段"
（实测），golden 与 baseline 会跑出同一份东西，#5 必要性恒为 0。

决定：

1. golden 臂的"加载"用 pi 自己的 `/skill:name` 展开：prompt = `/skill:<name> <用例文本>`，同时 `--skill <副本>`
   ——即 `agent-session._expandSkillCommand` 那条路径，与人在 TUI 里敲 `/skill:name` 是同一机制、同一字符串。
2. 两条臂**同 cwd、同用例文本**，唯一差别是有没有那个 skill 块：golden 带 `/skill:` 前缀 + `--skill`，
   baseline 用原文本 + `--no-skill`。
3. 沙箱分层：skill 副本放在 run 目录**之外**（否则 baseline 也能自己翻到 SKILL.md，A/B 失去意义），
   被评估的目标 skill 副本放在 `run/targets/` 内（用例只引用沙箱内路径，ADR-0018）。
4. T2 仍不跑（ADR-0018 不变）：触发精准度对这类 skill 没有载体，`#1` 保持 `skipped`。

原因：为什么不用 `--append-system-prompt` 塞 SKILL.md——那是自造第二套载体，字符串与位置都和
`/skill:name` 不同，测出来的不是真实加载路径；为什么不让 agent 自己 read SKILL.md——那会把"加载"
变成探索行为，baseline 同样做得到，A/B 就不再是"有 skill vs 无 skill"。

## Considered Options

- 把 SKILL.md 正文拼进用例 prompt：等价于自造载体，且没法解释"谁把它放进去的"。
- 干脆不做 T3、只留 T0/T1：那 #4/#5/#8/#9/#10/#12/#15 七条指标对 human 型 skill 永远空白。

## Consequences

- 载体绑 pi（别的宿主得各自实现 `/skill:name` 的等价展开）；报告 `meta.json` 要写明用了哪种加载方式。
- #5 必要性测的是"显式加载之后 skill 值多少"，不是"自动触发之后值多少"——两者不能互相替代。
- 改 `reference.md`/`SKILL.md`（写入载体说明）即改评估器指纹（ADR-0010）：本次改动之前的报告 `comparable=false`。
