# 可用性下一批：实现方案

状态：**Phase A–D 已实现并验收**。设计见 `usability-next.md`；本文件只写改哪些文件、接口、测试与顺序。

## Phase A：预估 + 进度 + 配置记忆

### 文件

- 新增 `scripts/estimate.py`
  - `estimate(tier, *, evalset=None, real_cases=0, real_expects=0, repeat=3, cached_stages=None) -> dict`
  - 只读路径/计数，不启动 run；纯函数，便于测试。
  - 返回 `calls/agent_runs/tokens/minutes/stages/weights/cached/assumptions`。
- `config.yaml` 新增 `estimate.*` 默认值：agent 秒、judge 秒、每次 agent token、judge token、T2A 早停比例；参与指纹。
- `web/app.py` 新增 `GET /api/estimate`：
  - query：`skill/tier/evalset/real_cases/real_expects/repeat`；
  - 只接受 `evalsets/` 下路径；错误 400；
  - 不需要 token、不扣配额、不建 run。
- `web/index.html`：
  - 运行区新增 `#estimateBar`，显示 calls/agent/tokens/minutes；输入变化防抖刷新。
  - `setPlan(tier, stages, weights)`；`plan` 事件没有 weights 时沿用 estimate 的 weights；`case` 进度继续使用阶段内比例。
  - `updateTiming` 使用 estimate 的 minutes；超过上限文案改为“这次比预估慢，可能原因…”。
  - `localStorage["se-form"]` 保存/恢复档位、重跑勾选、真实输入、模型/端点/协议、工作区/评测集；**不存 API key**。
- `tests/test_estimate.py` 新增：各档位计数、缓存裁剪、空评测集、T2A min/max、T3 repeat。
- `tests/test_web.py`：`/api/estimate` 计数、非法路径、localStorage 静态接线、动态 weights 静态接线。

### 验收

- 选 T2A、30 条触发集、0 条真实输入时，显示 ~30 agent 进程、0–30 次判定调用、按实际 case 数算的分钟区间。
- 选 T3、10 条 case、repeat=3 时，agent 进程 30，T3 权重显著大于 T2A。
- 刷新页面恢复配置，API key 不回填。
- 20 条真实输入 + 30 条触发集，不再显示固定的“2–45 分钟”。

## Phase B：报告文档化 + 建议汇总

### 文件

- `scripts/report.py`：`_advice` 每条增加确定性 `priority`（1/2/3）与 `effort`（quick/medium/large）；不改 16 指标 schema 与裁决。
- `web/index.html`：
  - 指标行 `id="m-N"`；筛选按钮（全部/失败/警告/未测）；`?run=...#m-N` 定位。
  - 统一 modal 管理：Esc 关闭、打开记录焦点、Tab 焦点锁、关闭恢复焦点；所有现有 modal 接入。
  - 结果区“下一步”卡：前 3 条建议 + 全部建议弹层；每条带 `#N` 反链。
  - `@media(max-width:640px)` 指标表转卡片，加 `data-label`，去掉横向滚动。
- `tests/test_report*.py`：advice 优先级/effort 稳定性。
- `tests/test_web.py`：锚点、筛选、modal 键盘、建议汇总、移动卡片静态接线；`#m-2` 深链。

### 验收

- `?run=...#m-2` 打开后定位并高亮。
- 只看失败/警告只改可见行，不重算指标。
- Esc/Tab 在弹窗内工作。
- 390px 无横向滚动。
- 建议汇总按 fail→warn→info 排序，前 3 条可直接跳指标。

## Phase C：审核体验

### 文件

- `web/app.py`：
  - `GET /api/evalset/items` 附带 `reviews/review.json` 的 `review`（verdict/note/reviewer/at）；无文件不报错。
  - 复用现有 `/api/evalset/status`（已返回 count/check），不在前端重算。
- `web/index.html`：
  - 评测集版本变化时加载 status，在审核表上方展示条数闸门 + 质量自检（照抄/重复/冲突/越界）。
  - 审核表默认“未审”，加搜索、组过滤、状态过滤、可见行全选、已审计数。
  - 批量通过二次确认，并显示质量自检摘要。
- `tests/test_web.py`：items 合并 review、默认未审、筛选/全选静态接线、质量面板接线。

### 验收

- 已审 12/30 刷新后仍是 12/30。
- 质量自检问题可见，能定位文件。
- 默认不提交“全通过”；提交前审核人必填。

## Phase D：单指标重跑（先 spike）

- 先按 `usability-next.md` 的 P2 门槛做测试：只发一个 rubric、其他 6 份 judge 字节不变、其他 15 条裁决不变、T1 缓存仍完整。
- 通过后新增 `runner --rerun-metrics`（只 T1），复用同一 results/归属/限流；不通过则关闭入口。

## 顺序与验收命令

1. A → `python -m pytest tests/test_estimate.py tests/test_web.py -q`
2. B → `python -m pytest tests/test_report*.py tests/test_web.py -q`
3. C → `python -m pytest tests/test_web.py -q`
4. 全量 `python -m pytest tests -q` + `python scripts/static_check.py .` + JS `node --check`
5. 父代理看 diff、跑冒烟后再提交；每阶段独立 commit。
