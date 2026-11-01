# skill-evaluator

一个用来评估其他 skill 的 skill —— 按 16 条质量指标产出机器可读的 `report.json` 和人可读的 `report.md`。

它本身是一个 skill（面向 [pi](https://github.com/earendil-works/pi-coding-agent)），不是一套前后端服务：复制到 skills 目录，指向任意 skill 包即可运行。

## 评估什么

16 条指标按**测量手段**分组，不是一条指标跑一次：

| 测量手段 | 指标 | 工具 |
|---|---|---|
| 静态检查 | #1 name/description 规范与字符预算（384 警告 / 1024 失败）、#3 调用方式、#4 危险命令 + 出站网络扫描、#2 正文行数（150 警告 / 500 失败）、#12 依赖全面性 | `scripts/static_check.py` |
| 沙箱运行 | #10 触发 P/R/F1、#13 成本统计、#11 必要性 A/B、#12 最小依赖、#14 结果可验证、#15 稳定性、#16 幂等 | `scripts/trace_run.py`、`score.py`、`trigger_judge.py`、`idem.py`、`compare.py` |
| LLM 评审 | #2 正文精简（语义两项，辅证）、#5 低冗余、#6 fallback、#7 前置自检、#8/#9 输入输出契约、#4 个人信息语义面（工号/客户名/内部系统） | `judges/*.md` rubric + `judge_runner.py` |
| 可信度 | #10 触发集质量（照抄 description / 重复 / 跨组冲突）、#15 跨模型一致率 | `scripts/evalset_check.py`、`model_robust.py` |

多个指标共用同一次沙箱运行——6 类运行场景按档位（T0 / T1 / T2A / T2B / T3，默认 T2A）增量编组，见 `skill-evaluator/reference.md`（§ 运行场景）与 ADR-0033。

## 目录结构

```
skill-evaluator/
├── SKILL.md            # 编排：分档流程（T0 / T1 / T2A / T2B / T3，默认 T2A），只放立即要做的事
├── reference.md        # 细则：档位定义、指标映射、沙箱定义、版本号、契约、fallback
├── config.yaml         # 可调阈值（384/1024 字符、150/500 行等）；参与评估器指纹
├── LICENSE             # MIT
├── requirements.txt    # 运行时零第三方依赖（标准库）；pytest 仅测试用
├── judges/             # LLM 评审 rubric（结构化 JSON 输出，固定单发 prompt）
├── scripts/            # CLI + runner + llm/harness 适配层；check-deps.sh = 前置自检（#7）
├── web/                # 本地前端（stdlib HTTP + SSE + 原生 JS）
└── tests/              # 330 个测试，全部走 subprocess seam
docs/adr/               # 38 条架构决策记录
```

评估器**自闭环**：脚本与测试全部在 skill 包内（ADR-0006）。评估产物（评测集、报告）放 `skill-evaluator/evalsets/<skill名>/`，按版本冻结——重跑不重新生成。

## 使用

1. 安装：把 `skill-evaluator/` 复制到 `~/.pi/agent/skills/`
2. 前置自检：`bash scripts/check-deps.sh`（python ≥ 3.9 / git / pytest / pi CLI / config.yaml 可解析）
3. 会话里说：*"评估这个 skill：`<路径>`"*；外部人员可在本地 UI 上传 `.zip`。
4. 或直接用确定性流水线 / 本地前端：
   ```bash
   python scripts/runner.py --skill <路径> --tier t2a
   python web/app.py    # http://127.0.0.1:8765
   ```
5. 拿到结果目录下的 `report.json` + `report.md` + `report.html` + `meta.json`
6. 页面里可检查/生成/审核评测集、测试 LLM 连接，并把 `fail` 人工降为 `warn`（写审计原因）

可选：

- `SKILL_EVAL_MODEL=<provider/id>`：固定 T2+ 沙箱运行用的模型（默认用 pi 当前模型）
- T1 直连 LLM：页面字段 > `config.yaml`（`llm.base_url` / `llm.model` / `llm.api_key_env`）> 标准环境变量（`OPENAI_API_KEY` / `ANTHROPIC_API_KEY`、`OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL`、`SKILL_EVAL_MODEL`）> 官方端点；页面 key 只用于本次运行，不落盘；本机 pi 可用 `llm.api_key_file` + `llm.api_key_provider` 从 `~/.pi/agent/auth.json` 读 key，不复制到 config。`--backend pi` 回退旧路径
- `SKILL_EVAL_REVIEW_MODEL=<provider/id>`：评测集 AI 审核用的模型（必须与生成阶段不同源；不设则首次运行交互选择）

档位入口：`--tier <t0|t1|t2a|t2b|t3>`（默认 T2A），高档复用低档产物（按被测 skill 的 content_sha256 判定）。

## 关键契约

- **报告**：16 个 key 恒定出现；没测的标 `skipped`（绝不打 0 分）；不合成加权总分，只给三档结论（`all-pass` / `with-warnings` / `has-failures`）；`tier` 字段记录跑到的最高档
- **评测集**：人工上传优先；自动生成的经 LLM 自动审核（换模型）即冻结，`reviewed_by: "ai"`，人工复核后可升级；冻结后跨版本复用（这正是跨版本对比可比的前提）
- **版本号**：git commit 短 hash；裸文件夹由评估器副本快照后标 `auto-<hash>`
- **LLM 评审输出**：必须过 `judge_runner.py --validate` 才能进报告；T1 直连 `llm` 层（无 tools），T2+ 走 `harness` 适配层
- **评估器指纹**：`report.json`（schema 2）与 `results/meta.json` 记录评估器自身的内容 sha256；指纹或 schema 不同的版本，横向差异不构成回归证据（ADR-0010/0032）

## 开发

```bash
cd skill-evaluator
python -m pytest tests        # 330 个测试全绿
```

设计决策及理由见 `docs/adr/`。英文版见 `README.md`。
