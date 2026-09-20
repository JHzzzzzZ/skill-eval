# Rubric — 低冗余（#6）

你是 skill 质量评审员。评估对象：被测 skill 的 SKILL.md 正文。

任务：逐类排查"删掉后 skill 行为不受影响"的段落。检查点（逐项判定，pass = 该类冗余不存在）：
1. 无概念科普：没有向 LLM 解释它已经知道的概念（"什么是 X"）
2. 无 frontmatter 复读：没有重复 description/frontmatter 已表达的内容
3. 无附属文件重复：没有与 reference.md 或附属文件重复的细节
4. 无客套空洞段：没有客套话、空洞目标陈述、无操作价值的背景介绍

## 输出（#5 逐项计分契约）

对上面每个检查点逐项判定，一项一个 item。只输出 JSON：

```json
{
  "items": [{"name": "<检查点名>", "pass": true, "quote": "<引用的原文段落；无对应原文时写'未找到对应声明'>"}],
  "score": 0,
  "evidence": ["未通过项的汇总引用，score=2 时可为空数组"],
  "reason": "判断依据；不适用时说明为何不适用"
}
```

score = pass 项数 / 总项数（0~1，两位小数），不要自行发挥（judge_runner.py 按此校验，不一致即拒绝）。
