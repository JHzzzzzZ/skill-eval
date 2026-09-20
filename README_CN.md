# skill-evaluator

一个用来评估其他 skill 的 skill —— 按 19 条质量指标产出机器可读的 `report.json` 和人可读的 `report.md`。

它本身是一个 skill（面向 [pi](https://github.com/earendil-works/pi-coding-agent)），不是一套前后端服务：复制到 skills 目录，指向任意 skill 包即可运行。

## 评估什么

19 条指标按**测量手段**分组，不是一条指标跑一次：

| 测量手段 | 指标 | 工具 |
|---|---|---|
| 静态检查 | #2 name/description 规范与 token 预算、#7 调用方式、#13 危险命令扫描 | `scripts/static_check.py` |
| 沙箱运行 | #1 触发 P/R/F1、#4 成本统计、#5 必要性 A/B、#8 最小依赖、#9 结果可验证、#10 过程审计、#12 稳定性、#15 幂等 | `scripts/trace_run.py`、`score.py`、`trigger_judge.py`、`idem.py`、`compare.py`、`process.py` |
| LLM 评审 | #3 正文精简、#6 低冗余、#11 fallback、#16 前置自检、#17/18 输入输出契约、#19 副作用可逆 | `judges/*.md` rubric + `judge_runner.py` |
| 历史对比 | #14 版本演进 | `scripts/evolution.py` |

多个指标共用同一次沙箱运行——5 类运行场景的合并方式见 `skill-evaluator/reference.md`（§ 运行场景）。

## 目录结构

```
skill-evaluator/
├── SKILL.md            # 编排：8 步流程，只放立即要做的事
├── reference.md        # 细则：指标映射、沙箱定义、版本号、契约、fallback
├── judges/             # LLM 评审 rubric（结构化 JSON 输出，temperature=0）
├── scripts/            # 确定性工具（每个脚本一个有文档的 CLI seam）
└── tests/              # 111 个测试，全部走 subprocess seam
docs/adr/               # 6 条架构决策记录
```

评估器**自闭环**：脚本与测试全部在 skill 包内（ADR-0006）。评估产物（评测集、报告）放 `skill-evaluator/evalsets/<skill名>/`，按版本冻结——重跑不重新生成。

## 使用

1. 安装：把 `skill-evaluator/` 复制到 `~/.pi/agent/skills/`
2. 会话里说：*"评估这个 skill：`<路径>`"*
3. 拿到 `skill-evaluator/evalsets/<name>/results/<version>/` 下的 `report.json` + `report.md`（`report.py --html` 另出单文件静态页：报告 + 评测集审核 + SVG 流程图）

可选：设置环境变量 `SKILL_EVAL_MODEL=<provider/id>` 固定沙箱运行和 LLM 评审用的模型（默认用 pi 当前模型）。

## 关键契约

- **报告**：19 个 key 恒定出现；没测的标 `skipped`（绝不打 0 分）；不合成加权总分，只给三档结论（`all-pass` / `with-warnings` / `has-failures`）
- **评测集**：人工上传优先；自动生成的必须人工审核后才冻结；冻结后跨版本复用（这正是 #14 版本演进可比的前提）
- **版本号**：git commit 短 hash；裸文件夹由评估器副本快照后标 `auto-<hash>`
- **LLM 评审输出**：必须过 `judge_runner.py --validate` 才能进报告

## 开发

```bash
cd skill-evaluator
python -m pytest tests        # 111 个测试全绿
```

设计决策及理由见 `docs/adr/`。英文版见 `README.md`。
