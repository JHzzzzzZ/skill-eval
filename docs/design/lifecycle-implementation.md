# 生命周期与可信度实现方案

状态：**已执行并验收（2026-11-01）**。设计见 `docs/design/lifecycle-and-privacy.md`。多用户认证/隔离不在本轮。

## A. 内容保真 + 进度 + 证据/审计/预检

### A1 内容保真

- `scripts/report.py`
  - `_public_text()` 删除 ADR 正则，只保留空白规范化。
  - 删除 `_public_obj()` 对整份 report 的递归清洗；`report.json` 的 `data` 原样保留。`_tables`/note 的空白规范化可以保留。
  - 加回归：static `description`/`errors`/judge evidence 含 `ADR-0002`，`report.json`、md、html 必须保留。
- `web/index.html`
  - `publicText()` 删除 ADR 正则，只做空白规范化。
  - 保留“UI 源码不引用 ADR”的静态测试，但不再测试“用户内容里不能出现 ADR”。

### A2 进度可信

- HTML：进度卡增加 `#timing`（已用/预计剩余）；CTA 增加 `#notifyDone` 复选框。
- JS：
  - 删除固定 `WEIGHT`，改为 `STAGE_WEIGHTS` + 当前档位阶段计划；按阶段权重和阶段内 case/repeat 比例推进。
  - `setProgress` 只增不减；新 run 才 reset。
  - 阶段 start 设当前阶段 0；case 事件按 `(index-1+arm)/total` 更新；done 设 1；skipped 也设 1。
  - `runStartedAt` + 1s interval 更新“已用 12:34 · 预计剩余 20–40 分钟”；超过上限显示“已超过预估，仍在运行”。
  - 标签页标题：运行中 `触发评测 · 12:34 · Skill Evaluator`；完成 `✓ 报告已生成`；停止/错误同步。
  - `#notifyDone` 勾选时请求 Notification 权限，完成/失败发通知；不默认弹权限。
- 不改变 SSE 已有心跳/重连。

### A3 证据、provenance、审核人、预检

- 后端 `/api/report`：在返回 JSON 上附加 `run_meta`（model/harness/skill_sha256/evalset）与 `run_id`；不改 report.json 契约。
- `report.py` 可选 `--model`/`--harness`，写入 report.json/meta，报告头显示评估器指纹、生成时间、模型、档位、审核人；runner 和 `_rerun_report` 传参。
- UI：
  - 指标行加“证据”按钮，打开 `#evidenceModal`：摘要、note、data JSON、该指标 advice；`<` 转义。
  - 结果头 `#provenance` 显示 run id、skill、tier、model、evaluator、generated_at、reviewed_by、partial。
  - 新增 `#reviewerName`（必填，localStorage 记忆）；override 与评测集审核都用它，后端拒绝空 reviewer。
  - 运行前 `preflightWarnings()`：T2A/T3 无评测集、T2B 无真实输入、未配置模型；`confirm` 列警告后继续，并在 `#runHint` 显示。
- 测试：报告保留 ADR；`/api/report` 带 run_meta；override 空 reviewer 400；UI 有 evidence/provenance/reviewer/preflight。

## B. 生命周期

### B1 评测集前置

- 把“评测集”卡移到“评估深度”卡之前，编号改为 `02 · 评测集（按需）`、`03 · 评估深度`。
- 深度卡 CTA 旁显示 `#runPrereq`：`本次使用 v1` / `未创建，将跳过 #10`；切换工作区/版本/档位时更新。
- 不改变生成/审核/导入导出逻辑。

### B2 完整历史与对比

- 后端 `GET /api/compare?base=<id>&target=<id>`：
  - 读两份 `report.json` + `run-meta.json`。
  - `comparable`：schema 相同且 evaluator content sha 相同；否则 false。
  - `skill_same`：`skill_sha256` 相同；不同也允许对比，但标“不同版本/不同 skill”。
  - `metrics`：每个 #N 的 base/target verdict、change（improved/regressed/same/skipped）、notes；counts。
- UI：最近报告卡加“查看全部历史”；`#historyModal` 列出最多 100 条，带两个单选/复选框；选两条 → 对比 → `#compareModal` 表格 + 不可比横幅。
- 测试：compare 正常、不可比、缺报告 404；历史弹层接线。

### B3 导出/分享

- 后端 `GET /api/export?id=<run>`：只允许合法 run id，返回 zip（report.json/md/html + meta.json；不含日志/原始 trace）。
- UI：结果头加“导出报告 .zip”和“复制链接”；复制 `location.origin + location.pathname + ?run=<id>`。
- 测试：zip 内容、路径穿越拒绝、复制链接静态接线。

## 验收

```bash
cd skill-evaluator
python -m pytest tests -q
python scripts/static_check.py .
python -m py_compile web/app.py scripts/runner.py scripts/report.py
```

冒烟：启动 web，跑 T0；停止 T1 看局部报告；打开历史/对比；导出 zip；检查进度不倒退、标题变化、ADR 内容保留。
