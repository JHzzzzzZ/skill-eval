# #13 静态面扩为五组规则；注入检测不走 LLM 判官

#13 原先只有 11 条 shell 正则（可移植性闸门另属 ADR-0008），对照外部同类工具的四类扫描（提示注入 / 已知恶意模式 / 硬编码凭据 / 结构校验），评估器只覆盖了"危险 shell"一类；`judges/` 七个 rubric 无一涉及安全面。这留下一个明显缺口：一个 skill 不需要任何 shell 权限，仅靠正文里的指令覆盖或隐藏字符就能越权，而流水线看不见。

决定：#13 = **五组静态正则**，命中级别分开。

| 组 | 内容 | 命中级别 |
|---|---|---|
| `dangerous` | 原有 11 条危险命令（rm -rf / sudo / curl\|sh …） | error → fail |
| `secrets` | 私钥 PEM、AWS/GitHub/Slack/OpenAI 前缀、JWT、高熵赋值（带占位符过滤） | error → fail |
| `injection` | 指令覆盖短语（中英）、"不要告诉用户"、伪 system 段、零宽/双向控制字符 | error → fail |
| `exfil` | curl -d/-X POST、requests.post、nc -e、读 `.ssh`/`.aws/credentials`/`.env` | error → fail |
| `obfuscation` | base64/eval 组合、8+ 连续 `\xNN`、120+ 字符疑似 base64 blob | warning → warn |

**注入检测不挂 LLM 判官**：ADR-0004 规定一条指标恰属一种测量手段，#13 已归静态检查，再挂一个 rubric 会让同一指标跨手段——分数不可比，`evolution.py` 的跨版本 diff 失真。而且注入的确定性特征（指令覆盖短语、不可见字符）不需要语义理解，正则足够。被拒绝的另外两条：新增第 20 条指标（破坏 ADR-0004 的 19 键契约与前端映射）；把注入挂到 #16/#19（语义不符：#16 是环境前置校验，#19 是副作用可逆）。混淆不判 fail：内嵌数据、压缩常量都长得像 base64，一票否决太脆。

## Consequences

- `static.json` 新增 `secrets` / `injection` / `exfil` / `obfuscation` 四个字段（与 `dangerous` 同构，每行最多记一条）；`report.py` 的 #13 `data` 从列表变为五组字典，verdict 规则 = 前四组任一命中 fail / 仅混淆命中 warn / 全空 pass
- error 文案里不得出现 name/description/invoke 字样——`report.py` 用子串把 errors 分流到 #2/#7，有测试锁住
- 覆盖率局限（要在报告里如实说）：高熵自造 token 认不出；改写的语义注入、跨段拼接的注入漏检；`.env` 读取只在出现读取动词时命中
- 强制拦截仍然做不到（reference.md § 权限边界），兜底是 trace 审计 + 人工复核
