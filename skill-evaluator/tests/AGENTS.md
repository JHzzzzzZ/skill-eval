# PROJECT KNOWLEDGE BASE — skill-evaluator/tests

Generated: 2026-09-22T13:43:08.578Z

## OVERVIEW

26 个测试文件 / 398 个 test，全部走 subprocess seam：把脚本当 CLI 跑，不 import 被测脚本（`test_trace_run.py` 例外，它 import `EventAggregator` 做增量消费断言；`test_config.py` 直接 import `_config.py`；`test_runner.py` 直接 import `_real_cases`/`stage_ready` 做判定断言；`test_web.py` 直接 import `web/app.py`，审核人 400 与访问门禁用例临时起 loopback HTTP 服务；`test_token_admin.py` 用 `importlib` 加载 `web/token_admin.py` 测纯函数 + 跑 CLI）。

## WHERE TO LOOK

| 测试文件 | 覆盖 | test 数 |
|---|---|---|
| `test_static_check.py` | `scripts/static_check.py`（六组规则 + PII + 依赖全面性 + 字符/行阈值） | 63 |
| `test_config.py` | `scripts/_config.py`（YAML 子集解析/优先级/类型） | 7 |
| `test_evalset_gen.py` | `scripts/evalset_gen.py`（prompt/条目归一化） | 2 |
| `test_web.py` | `web/app.py`（zip 上传安全边界 / UI 术语 / 停止进程 / 复用拷贝与复用归属 / 工作区 / 评测集版本与导入导出 / 磁盘 history 回退 / 局部报告 / ACTIVE 运行态 / 内容保真 / provenance / 审核人 400 / 预检接线 / 评测集前置 / 历史对比与可比性判定 / 报告导出 / T2-T3 token 门禁与 run 归属 / 游客 T0-T1 / legacy 在无 token 时才放行 / 审计） | 45 |
| `test_token_admin.py` | `web/token_admin.py`（issue 只返一次明文、只落 hash / list 不泄 hash / revoke 即时失效 / CLI 全局 `--tokens-file`/`--audit-file` / 路径不走环境变量 / 审计写失败不抛 / 损坏库 fail closed） | 9 |
| `test_llm.py` | `scripts/llm.py`（协议/无 tools/JSON 提取） | 5 |
| `test_judge_run.py` | `scripts/judge_run.py`（文件收集/prompt/SKILL.md 优先） | 5 |
| `test_runner.py` | `scripts/runner.py`（dry-run/T0 端到端 / 进度计时 / T0-T1 复用与跨目录缓存 / 阶段级完成判定 / 半截 T1 不缓存 / 真实输入解析） | 14 |
| `test_report.py` | `report.py` 16-key 契约基线 + 局部报告 + 徽章中文化 + 内容保真 / 报告头 provenance | 57 |
| `test_trace_run.py` | `scripts/trace_run.py` | 44 |
| `test_report3.py` | `report.py` 元数据/指纹批次（ADR-0010/0011）+ data 渲染层 | 21 |
| `test_report4.py` | `report.py` 报告可用性（表格化/折叠/闸门可见/建议/✓✗/依赖全面性/PII，ADR-0029/0030/0031） | 19 |
| `test_report5.py` | 报告页徽章对比度与中文可见文案回归 | 6 |
| `test_score.py` | `scripts/score.py` | 16 |
| `test_judge_runner.py` | `scripts/judge_runner.py` | 13 |
| `test_evalset_check.py` | `scripts/evalset_check.py`（触发集质量） | 13 |
| `test_model_robust.py` | `scripts/model_robust.py` | 12 |
| `test_trigger_judge.py` | `scripts/trigger_judge.py` | 9 |
| `test_report2.py` | `report.py` 断链修复批次 | 5 |
| `test_evalset_count.py` | `scripts/evalset_count.py`（条数闸门） | 8 |
| `test_subprocess.py` | `scripts/_subprocess.py`（Windows 路径解析/进程树） | 7 |
| `test_compare.py` | `scripts/compare.py` | 6 |
| `test_idem.py` | `scripts/idem.py` | 4 |
| `test_console.py` | `scripts/_console.py`（GBK 控制台降级） | 4 |
| `test_calibrate.py` | `scripts/calibrate.py`（一致率/准确率统计 / 校准集结构） | 3 |
| `test_pairwise.py` | `scripts/pairwise.py`（A/B prompt 匿名化） | 1 |

`scripts/` 16 个 Python CLI + 5 个共享模块全覆盖，无缺口；`web/` 的 `app.py` 由 `test_web.py` 覆盖、`token_admin.py` 由 `test_token_admin.py` 覆盖。T4 的 3 个测试已随脚本移入 `attic/tests/`。`report.py` 是唯一拆成 4 个文件覆盖的脚本。`check-deps.sh` 无 Python 测试（前置自检靠实跑验证）。

## CONVENTIONS

- **subprocess seam**：`subprocess.run([sys.executable, str(SCRIPT), ...])` → 断言 `returncode == 0, f"script failed: {r.stderr}"` → 再 `json.loads(r.stdout)` 或读落盘文件。
- 顶部常量：`SCRIPT = Path(__file__).parent.parent / "scripts" / "<name>.py"`。
- 顶部中文 docstring 第一行写 seam：`"""Seam: python scripts/xxx.py <args> -> stdout JSON"""`，下接输入/输出契约。
- 纯 pytest + `tmp_path`。无 `pytest.ini`、无 `unittest`、无 `mock`/`monkeypatch`。唯一的 `conftest.py` 只干一件事：把 `PYTHONIOENCODING` 设为 `utf-8`（否则 GBK 控制台下子进程 stdout 解码炸棹，46 个用例集体假阴性）。
- 打桩 = 把 canned JSON 写进 `tmp_path`（伪造 `judges/<metric>.json`、`score.json`、trace）。**不调真实 LLM、不跑 pi CLI**。
- 命名 `test_<行为>`；文件内用 `# --- Slice N: <主题> ---` 分段。
- 同一脚本的新特性批次**追加到新文件**（`test_report2.py`、`test_report3.py`、`test_report4.py`），不改基线文件：基线文件锁契约，批次文件锁回归。若新需求**故意改写契约**（如 ADR-0030 把“无表就留 JSON 指针”改成“一律表格化”），在旧文件里就地改写并**在注释里写明为什么**（不能默默改断言）。
- 断言渲染结果时注意引号：`_HTML_TMPL` 里的属性用双引号（`id="t-report"`），指标卡与前端拼接的 HTML 用单引号（`id='m-13'`）。
- 要测纯函数（如 `evaluator_fingerprint`）时用 `importlib.util.spec_from_file_location` 加载模块——这是唯一允许的"import 被测脚本"形式。

## ANTI-PATTERNS

- 不 `import` 被测脚本后直调 `main()`：脚本的契约是 CLI + stdout JSON，绕过 seam 就测不到契约（15/15 文件都走 subprocess）。
- 不引入 `conftest.py` / `pytest.ini` / `pyproject.toml` 来共享 fixture——本仓无任何测试配置，`python -m pytest tests` 必须零配置可跑。
- 不让测试真调 LLM 或 pi CLI；`--events` / `--build-only` 就是为此存在的离线 seam。
- 不在测试里依赖或修改 `tests/tmp-ev/`（见 NOTES）。

## NOTES

- `tests/tmp-ev/triggers/should/s1.json`（内容仅 `{"prompt": "x"}`）已被 git 跟踪但全仓零引用，是误提交的 scratch 残渣。
- `test_static_check.py::test_evaluator_self_check_passes` 是自检回归锁：保证 `python scripts/static_check.py skill-evaluator` 输出 `passed: true`（ADR-0012/0013）。
- `test_web.py::test_override_rejects_empty_reviewer` 是本仓最早的临时 HTTP 服务用例（端口 0 + daemon 线程，结束 `shutdown()`）；新增的访问门禁用例共用 `web_server`/`call` 帮手，并用 `_FakeProc` 替掉 `subprocess.Popen`（不真跑 runner），否则每个用例都会拉起真实子进程。
- `test_web.py` 里带非 ASCII 字符的绝对路径不能直接拼进请求行（`http.client` 按 ASCII 编码）：访问门禁用例改用固定的 ASCII 评测集名，避免 Windows 中文用户名环境假阴性。
- 该文件是最大测试文件（63 test），因 #4 扩为六组规则后每组都要正向+负向用例。加规则时必须同时加“不该误报”的负向用例（尤其 `secrets` 的占位符、`obfuscation`/`pii` 的 warn 分级与示例白名单）。
