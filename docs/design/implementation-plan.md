# 实现方案（已执行）

状态：**已执行并验收（2026-11-01）**。本文件是步骤 2 的实现方案记录。
当前工作区已有未提交改动：`runner.py` T1 阶段级缓存、`report.py` 局部报告、`web/app.py` 版本 helper。**保留这些改动，在其上继续，不要 reset。**

## 0. 不可破坏的约束

- 运行时零第三方依赖；只用 stdlib。
- 用户可见文本不出现 `ADR`、`T0/T1/T2A/T2B/T3`、`harness`、`backend`。
- 检查点只在阶段边界；T1 必须 7 个 rubric 全齐才写缓存；半截 T1/T2A/T2B/T3 不续跑。
- 停止后只用最高完整阶段出局部报告；当前半截阶段不参与。
- 不覆盖冻结评测集；导入冲突自动新建 `vN`。
- 所有上传/导入走现有安全解压；不引入新依赖。
- 子 agent 不 `git commit` / `git push`；父 agent 验收后提交。

## 1. 后端

### 1.1 runner.py

- 保留已改的 T1 阶段级逻辑。
- 验证 `_judges_ready` 只在 7 个 rubric 全齐时返回 True。
- T2A/T2B/T3 缓存只在阶段完成后写；阶段开始只恢复完整缓存。
- 新增/复用阶段完成判定时，必须校验 JSON 可读且必要字段存在，不能只看文件存在。
- 测试：
  - T1 只有 3 个 judge 时，缓存不写；下次 7 个全跑。
  - T1 7 个全齐时，跨 results 复用。
  - 半截 T2A/T2B/T3 不被当作完整缓存。

### 1.2 report.py

- 已加：`--partial`、`--completed-stage`、`--completed-stages`、`partial`、`completed_stages`、`stopped_at`、中文结论、T2B 补数据。
- 补：
  - `--partial` 时 `report.json` 顶层必须有 `partial: true`、`completed_stages`、`stopped_at`。
  - `build_meta()` 同步写 `partial` / `completed_stages` / `stopped_at`。
  - `report.html` 顶部显示“局部报告：仅展示已完成阶段”。
  - `report.md` 同样显示。
  - 停在 T3 时从 `t2b/score.json` 补 `necessity/match/preference`，不把 T3 半截数据算进去。
- 测试：
  - 空 results + `--partial --completed-stage t0`：16 key 仍在，`partial=true`，html/md 有标识。
  - 有 T2B score、无根 score 时，局部 T2B 报告 #11 不是 skipped。
  - 中文结论出现在 html/md，`report.json` 仍保留 code。

### 1.3 web/app.py：评测集版本

- 保留 `_evalset_versions` / `_evalset_fields`，补：
  - `_evalset_base` 统一用 `EVALSETS`。
  - 每个版本返回 `version/path/frozen/reviewed_by/skill_sha256/source`；`source` 为 `current|old|unknown`。
  - `GET /api/workspaces` 每项带 `evalset`、`evalset_version`、`evalset_versions`。
  - 上传响应同样带这些字段。
- 测试：
  - 同内容上传返回 v1；建 v2 后默认 v2；版本列表含 source。
  - skill hash 不同的旧集标 `old`。

### 1.4 web/app.py：导入/导出

- `GET /api/evalset/export?evalset=<path>`：
  - 校验路径在 `EVALSETS` 下。
  - zip 根含 `meta.json`、`triggers/`、`cases/`、可选 `reviews/`；排除 `results/`。
  - 文件名 `evalset-<name>-<version>.zip`。
- `POST /api/evalset/import?filename=x.zip`：
  - `_receive_zip(max_bytes=MAX_EVALSET_BYTES, subdir="eval-uploads")`。
  - `_safe_extract(max_zip_bytes=MAX_EVALSET_BYTES, max_files=MAX_EVALSET_FILES)`。
  - 自动定位 zip 根（`meta.json` 或 `triggers/`/`cases/`）。
  - 名称优先 `meta.json.name`，否则文件名；版本优先 `meta.json.version`，否则 `v1`。
  - 目标已存在 → 自动下一个 `vN`，绝不覆盖。
  - 写 `imported_at`、`source_archive`。
- 前端按钮：导出当前版本 / 导入 zip；导入后刷新版本列表。
- 测试：round-trip、重复导入生成 v2、zip slip、超大/文件数限制、无 meta 文件回退。

### 1.5 web/app.py：运行历史与磁盘回退

- `GET /api/runs?limit=20`：扫 `web/runs/*/results/run-meta.json`，返回：
  ```json
  {"runs":[{"id","skill_name","tier","created_at","conclusion","partial","stopped","running","has_report"}]}
  ```
  按 `created_at`/mtime 倒序；包含 ACTIVE。
- `/api/status?id=`：ACTIVE 没有时回退磁盘；返回 `running:false`、`returncode`、`stopped`、`partial`、`results`、`report`。
- `/api/report?id=` 与 `/report?id=`：ACTIVE 没有时回退 `web/runs/<id>/results/`；只有确实不存在才 404。
- `GET /api/log?id=`：返回 `{"id","log"}`，尾部 200 行；ACTIVE/磁盘都可读。
- 测试：磁盘旧 run 能读 status/report/log；不存在 404；列表含结论。

### 1.6 web/app.py：停止后局部报告

- `_completed_stages(results)` 按产物判定：
  - T0：`static.json` 可读且 `passed` 是 bool
  - T1：7 个 judge 全齐（复用 `runner_mod._judges_ready`）
  - T2A：`triggers.json` + `score.json` 可读
  - T2B：`t2b/score.json` + `t2b/runs.json` + `t2b/baseline.json`
  - T3：`runs.json` + `score.json` + `idem.json`
- `_finalize_partial(run)`：取最高完整阶段；没有 T0 返回错误；调用 `_rerun_report(..., partial=True, completed_stages=...)`。
- `_rerun_report` 支持 `partial`/`completed_stages`，用最高阶段作 `--tier`，并传 `--partial --completed-stages`。
- `/api/stop` 杀进程后生成局部报告；响应带 `report`、`partial`、`completed_stages`、`error`；SSE `stopped` 事件带 `report`。
- 前端停止后显示“已停止，查看已完成阶段报告”，不再只弹终止 toast。
- 测试：预置 T0/T1 + 半截 T2A，停止后 report 存在、`partial=true`、`#10=skipped`；只有半截 T0 不生成空报告。

### 1.7 web/app.py：SSE 与错误

- `/api/events` 去掉 60 分钟硬截止；每 15 秒发 SSE 注释心跳，进程结束/停止才收尾。
- `runner exited` 错误带 `log` 路径提示。
- 前端 `es.onerror` 不直接 `finish()`；先查 `/api/status`：
  - running → 常驻错误条“连接中断，仍在运行”，指数退避重连。
  - 已结束且有报告 → 加载报告。
  - 已结束无报告 → 常驻错误条 + 查看日志。
- 测试：状态回退、日志接口；SSE 不因 60 分钟自动结束（代码断言/短超时模拟）。

## 2. 前端 index.html

### 2.1 评测集版本

- 删除 `#evalOut` 输入框；新增 `#evalVersion` 下拉。
- `loadWorkspaces()` 保存每个 workspace 的版本数组；工作区下拉文案 `名称 · hash8 · v1`。
- 选中工作区后填版本下拉；版本项显示 `v1 · 当前版本` / `v1 · 基于旧版本` / `v1 · 来源未知`。
- 所有生成/审核/运行使用 `currentEvalset`，不再读路径输入。
- 生成后刷新版本并选最新。
- 导入/导出按钮接到 1.4。

### 2.2 运行历史 / 深链

- 页面加载读 `?run=<id>`：先 `/api/status`，运行中则 `listen`，已结束则 `showReport`。
- 开始运行时 `history.replaceState` 写 `?run=`；`localStorage` 存最近 run。
- 右栏新增“最近报告”列表，来自 `/api/runs`；点击加载。
- 首页/右栏空态显示最近一次报告；无报告时显示“还没有报告”。

### 2.3 CTA / 右栏

- “开始评估”移到 02 卡片顶部（档位上方）；底部保留停止按钮。
- 右栏进度卡 `position:sticky; top:18px`；空态放最近报告/预计耗时，不留大块空白。
- 1366×768 首屏可见主按钮。

### 2.4 档位信息

- 每个档位 label 增加：预计耗时、LLM 调用数、产出指标；T2A 标“推荐”。
- 文案用范围，不写死单值；例如：
  - 基础检查 `5–15 秒 · 0 次模型 · #1/#3/#4`
  - 文档评审 `1–2 分钟 · 7 次模型 · #2/#5–#9`
  - 触发评测 `2–45 分钟 · 按 case 兜底 · #10` + 推荐
  - 真实输入 `5–40 分钟 · 2×case · #11/#12`
  - 完整运行 `30 分钟–数小时 · 重复×3 · #13–#16`

### 2.5 术语 / 文案

- 结论映射：`all-pass → 全部通过`、`with-warnings → 有警告`、`has-failures → 有失败`；报告页同样。
- `执行载体` → `运行方式`；`T1 评审后端` → `评审方式`；选项不显示 `pi/backend/harness` 这些词。
- Hero 删除“外部使用者不需要配置 Python 路径”，改成“本地启动后上传 zip；同一内容自动复用”。
- 用户可见区域不出现 `T0/T1/T2A/T2B/T3`；进度/报告分组用中文标签。

### 2.6 错误条 / 日志

- 新增常驻 `#errorBar`（错误 + 查看日志 + 重连），不再只靠 4 秒 toast。
- 新增日志弹层 `#logModal`，调 `/api/log`，显示尾部 200 行。
- SSE 重连逻辑按 1.7；超时不把 UI 打回“待机”。
- 停止确认文案改成“已完成的阶段会保留，并生成局部报告”。

## 3. 测试与文档

- `tests/test_runner.py`：T1 半截不缓存 / 全齐缓存；阶段级检查点。
- `tests/test_report.py`：partial 字段、中文结论、T2B fallback、html/md 标识。
- `tests/test_web.py`：
  - 版本列表/source
  - import/export round-trip、重复版本、zip slip
  - `/api/runs`、磁盘 status/report/log
  - `_completed_stages` 与停止局部报告
  - 前端无 `evalOut`、无英文结论、有 `errorBar`/`recent`
- 文档：`reference.md` 补 partial/checkpoint 语义；`SKILL.md` 流程同步；`AGENTS.md` 计数；设计文档标“已实现”。
- 验收命令：
  ```bash
  cd skill-evaluator
  python -m pytest tests -q
  python scripts/static_check.py .
  python -m py_compile web/app.py scripts/runner.py scripts/report.py
  ```
- 冒烟：启动 `python web/app.py`，上传/选工作区、跑 T0、停止、刷新 `?run=`、打开历史/日志。

## 4. 提交边界

子 agent 完成全部代码与测试后停止，不提交。父 agent：
1. 看 diff。
2. 跑定向测试。
3. 跑全量 + static + py_compile。
4. 冒烟。
5. 通过后按逻辑分 2–3 个 commit 提交并 push。
