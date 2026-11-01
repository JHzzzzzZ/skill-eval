# 待实现设计（2026-11-01）

状态：**待审核，未实现**。实现必须按根 `AGENTS.md § IMPLEMENTATION WORKFLOW`：设计 → 实现方案 → 子 agent → 父 agent 测试验收。

## A. 评测集与缓存

### #13 移除评测集目录输入，自动识别版本

目标：用户不再手填 `evalsets/<name>/vN`；工作区显示 `名称 · hash8 · v1`，版本可切换。

设计：

- 工作区 ID 不变：`<slug>-<skill_hash8>`。
- `GET /api/workspaces` 每项增加：
  ```json
  {"evalset":".../evalsets/name/v1",
   "evalset_version":"v1",
   "evalset_versions":[{"version":"v1","path":"...","frozen":true}]}
  ```
- `_evalset_for(name, sha)` 仍返回最新版本；新增 `_evalset_versions()` 只读目录。
- 前端删除 `evalOut` 输入框，改成“评测集版本”下拉；工作区下拉文案追加 `· v1`。
- `currentEvalset` 由工作区/版本选择驱动；生成、审核、运行都只读这个状态。
- 没有评测集时显示 `v1（未创建）`，生成草稿写到该路径。

边界：`name` 改变则不自动复用；旧版本路径不展示给用户，只展示版本号。

验收：上传同内容 skill → 显示 v1；存在 v2 → 默认最新且可切回 v1；页面无评测集路径输入；运行使用所选版本。

### #14 评测集导入/导出

格式：zip，根目录包含 `meta.json`、`triggers/{should,should-not,confusable}/*.json`、`cases/*.json`，可选 `reviews/`；排除 `results/`。

导出：

- `GET /api/evalset/export?evalset=<path>`。
- 校验路径必须在 `evalsets/` 下。
- 内存打 zip，返回 `application/zip`，文件名 `evalset-<name>-<version>.zip`。

导入：

- `POST /api/evalset/import?filename=x.zip`，原始 zip 上传，复用安全解压。
- 单独限制：50MB / 500 文件，避免沿用 1GB skill 上限。
- 自动定位 zip 根（`meta.json` 或 `triggers/`）；校验至少一个触发组或 cases。
- 名称优先 `meta.json.name`，否则 zip 文件名；版本优先 `meta.json.version`，否则 `v1`。
- 目标已存在 → 新建下一个 `vN`，绝不覆盖冻结集。
- 写入 `imported_at` / `source_archive` 到 `meta.json`；返回新路径和版本列表。

前端：评测集卡片加“导入 .zip / 导出 .zip”；导入后刷新版本下拉。

验收：导出再导入 round-trip 内容一致；重复导入自动生成 v2；zip slip/超大包被拒绝；冻结集不被覆盖。

### #15 评测集旧版本提示与版本选择器

设计：

- 新生成的 `meta.json` 记录 `skill_name`、`skill_sha256`、`created_at`。
- 工作区响应的每个版本带 `skill_sha256`；与当前 workspace 的 skill hash 比较。
- 版本下拉显示：`v1 · 基于旧版本` / `v2 · 当前版本` / `v1 · 来源未知`。
- 不阻止运行，只提示；报告 meta 记录所选评测集版本与 hash。
- 旧评测集没有 hash 时显示“来源未知”，不猜。

验收：skill 改版后旧评测集仍可选，但明确标“基于旧版本”；新评测集标“当前版本”。

### #16 验证阶段级检查点

这是验收项，不新增产品逻辑。检查点只放在**阶段边界**，不做 rubric/case 级续跑。

1. 停在 T1：T0 已缓存；T1 未完成，缓存不写。下次 T1 整体重跑。
2. 停在 T2A：T0/T1 已缓存；T2A 未完成，缓存不写。下次 T2A 整体重跑。
3. 停在 T3：T0/T1/T2A/T2B 已缓存；T3 未完成，缓存不写。下次 T3 整体重跑。
4. 断言：不读取半截 T1/T2A/T3 结果；不把部分 rubric/case 当已完成。

### #18 阶段级检查点与中断续跑

目标：中断时只保留**完整阶段**，当前阶段整体重跑；不逐 rubric、不逐 case 做检查点。

规则：

- 检查点只在阶段完成后写：
  - T0 完成 → 缓存 `static.json`
  - T1 完成（7 个 rubric 全齐）→ 缓存 `judges/` + `raw/llm-judge/`
  - T2A 完成 → 缓存 `triggers.json` / `score.json` / `triggers/` / `trigger-judge/`
  - T2B 完成 → 缓存 `t2b/` / `real-input.meta.json` / `golden.json`
  - T3 完成 → 缓存 `runs.json` / `idem.json` / `compare.json` / `score.json` / `traces/`
- 阶段开始只恢复**完整阶段**缓存；当前阶段的部分产物不参与续跑。
- 中断发生在 T1/T2A/T2B/T3 中间：该阶段缓存不写，下次整档重跑；之前完整阶段照常复用。
- 现有 T1 逐 rubric 落盘是过度设计，必须回退为“7 个全齐才写缓存”。
- T2A/T2B/T3 已按整档缓存；补上阶段完成判定与失败时的缓存清理，避免半截产物被误认为完整。

验收：停在 T1 → 只复用 T0；停在 T2A → 复用 T0/T1；停在 T3 → 复用 T0/T1/T2A/T2B；部分阶段结果不被读取。

### #25 停止后生成已完成阶段的局部报告

目标：在任意阶段停止，都给已完成阶段出报告；当前阶段没跑完就标未完成，不拿半截数据冒充完整结论。

设计：

- 停止时不只看进程退出，还要做一次“完成度判定”。判定按产物，不按进度日志：
  - T0：`static.json` 合法
  - T1：7 个 `judges/*.json` 全部合法
  - T2A：`triggers.json` + `score.json` 完整
  - T2B：`t2b/score.json` + 两臂数据完整
  - T3：`runs.json` + `score.json` 完整
- 取**最高完整阶段**作为报告档位；当前半截阶段不算。
  - 停在 T2A → 用 T1 档位出报告，T2A/T2B/T3 标 skipped
  - 停在 T3 → 用 T2B 档位出报告，T3 标 skipped
- 如果连 T0 都没完成：不生成报告，返回明确错误，不产生空报告。
- `report.py` 增加 `--partial --completed-stage <t>`（或等价参数）：
  - `report.json` 顶层写 `partial: true`、`completed_stages`、`stopped_at`
  - 报告页顶部显示“已停止：仅展示已完成阶段”
  - 结论仍按已完成指标计算，但不得被误读为完整结论
- `/api/stop` 在进程退出后调用局部报告生成；SSE 的 `stopped` 事件带 `report` 地址。
- 前端停止后显示“已停止，查看已完成阶段报告”，不再只显示终止 toast。

边界：T1 只完成部分 rubric → T1 不算完整，报告只到 T0；T1 缓存不写，下次整档重跑。T2A/T2B/T3 的部分 case 同理。

验收：停在 T2A 有 T0/T1 报告且 T2A skipped；停在 T3 有 T0–T2B 报告；T0 未完成不生成空报告；报告页有“局部报告”标识。

## B. 五个体验问题

### #20 刷新即失忆 / 历史入口

设计：

- 新增 `GET /api/runs?limit=20`：扫描 `web/runs/*/results/run-meta.json`，返回 id、skill 名、tier、created_at、conclusion、是否有 report、是否 active。
- `/api/report?id=` 与 `/report?id=`：先查 ACTIVE，再回退磁盘 results；只有磁盘也没有才 404。
- 前端启动解析 `?run=<id>`；有则恢复报告/进度。运行开始时 `history.replaceState` 写 URL，并写 `localStorage` 最近 run。
- 右栏空态/首页显示“最近一次报告”，可打开历史列表。

验收：刷新不丢当前 run；旧 run 可直接打开；历史列表能看到磁盘上的 run；404 只出现在确实不存在时。

### #21 首屏 CTA 与右栏空态

设计：

- “开始评估”移到 01 卡片上方；或桌面端吸底操作条，移动端普通流。
- 右栏进度卡 `position:sticky`；无运行时显示最近报告、示例报告、预计耗时。
- 1366×768 下 CTA 首屏可见；右栏不再出现 900px 空白。

验收：1366×768 不滚动能看到主按钮；右栏空态有内容；不与顶部/底部遮挡。

### #22 档位耗时、成本、指标

设计：每个档位显示：

| 档位 | 预计 | LLM 调用 | 产出 |
|---|---|---|---|
| 基础检查 | 5–15s | 0 | #1 #3 #4 #12 静态面 |
| 文档评审 | 1–2min | 7 | #2 #5–#9 |
| 触发评测 | 2–45min | 0–30 次兜底 | #10 |
| 真实输入 | 5–40min | 2×case + 判定 | #11 #12 |
| 完整运行 | 30min–数小时 | 重复×3 | #13–#16 |

标注“推荐 T2A”；所有时间为范围，注明取决于模型/评测集/case 数，不写死单值。

验收：档位卡可见耗时/调用/指标；默认推荐明确；不出现虚假精确时间。

### #23 用户可见术语中文化

设计：

- 结论映射：`all-pass → 全部通过`、`with-warnings → 有警告`、`has-failures → 有失败`。
- 用户层不显示 `T0/T1/T2A/T2B/T3`、`harness`、`backend`；收进“高级设置”，显示“评审方式/运行方式”。
- Hero 删除“不需要配置 Python 路径”这类不实文案，改成“本地启动后上传 zip”。
- 报告页同步映射，不只在首页改。

验收：可见文本无英文枚举/内部缩写；高级区仍可配置；文案与实际启动方式一致。

### #24 出错即失联

设计：

- 常驻错误条：显示 run id、错误、`查看日志`；不自动消失。
- `GET /api/log?id=`：返回 `run.log` 尾部 200 行；旧 run 也可读。
- SSE 不再硬编码 60 分钟；断线后先查 `/api/status`：
  - 仍在跑 → 显示“仍在运行，重连中”，指数退避重连。
  - 已结束 → 加载报告。
  - 不存在 → 显示错误条。
- 超时/断线不把 UI 打回“待机”；保留 `?run=`，刷新可恢复。

验收：runner 崩溃有常驻错误和日志入口；SSE 断开后任务继续时 UI 能重连；不会因误判结束而重复启动 run。

## 实施顺序

1. #13 + #15（评测集版本 UX）
2. #14（导入/导出）
3. #18（逐 case 检查点）
4. #25（停止后的局部报告）
5. #20（历史/深链）
6. #24（错误/重连）
7. #21（首屏布局）
8. #23（术语）
9. #22（档位信息）
10. #16（T1 中断验收）
11. #17（文档、测试、提交）

粗估总计 12–16 小时；每项单独走子 agent 实现，父 agent 验收后提交。
