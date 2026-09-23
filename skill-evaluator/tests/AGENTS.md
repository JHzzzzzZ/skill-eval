# PROJECT KNOWLEDGE BASE — skill-evaluator/tests

Generated: 2026-09-22T13:43:08.578Z

## OVERVIEW

16 个测试文件 / 229 个 test，全部走 subprocess seam：把脚本当 CLI 跑，不 import 被测脚本。

## WHERE TO LOOK

| 测试文件 | 覆盖 | test 数 |
|---|---|---|
| `test_static_check.py` | `scripts/static_check.py`（含五组规则） | 40 |
| `test_report.py` | `report.py` 19-key 契约基线 | 22 |
| `test_report3.py` | `report.py` 元数据/指纹批次（ADR-0010/0011）+ data 渲染层 | 25 |
| `test_trace_run.py` | `scripts/trace_run.py` | 17 |
| `test_model_robust.py` | `scripts/model_robust.py` | 12 |
| `test_score.py` | `scripts/score.py` | 11 |
| `test_evalset_check.py` | `scripts/evalset_check.py`（触发集质量） | 11 |
| `test_evalset_count.py` | `scripts/evalset_count.py`（条数闸门） | 11 |
| `test_judge_runner.py` | `scripts/judge_runner.py` | 8 |
| `test_evolution.py` | `scripts/evolution.py` | 8 |
| `test_ablation.py` | `scripts/ablation.py` | 5 |
| `test_compare.py` | `scripts/compare.py` | 5 |
| `test_report2.py` | `report.py` 断链修复批次 | 5 |
| `test_idem.py` | `scripts/idem.py` | 4 |
| `test_process.py` | `scripts/process.py` | 3 |
| `test_trigger_judge.py` | `scripts/trigger_judge.py` | 3 |

`scripts/` 14 个 Python 脚本全覆盖，无缺口。`report.py` 是唯一拆成 3 个文件覆盖的脚本。`check-deps.sh` 无 Python 测试（前置自检靠实跑验证）。

## CONVENTIONS

- **subprocess seam**：`subprocess.run([sys.executable, str(SCRIPT), ...])` → 断言 `returncode == 0, f"script failed: {r.stderr}"` → 再 `json.loads(r.stdout)` 或读落盘文件。
- 顶部常量：`SCRIPT = Path(__file__).parent.parent / "scripts" / "<name>.py"`。
- 顶部中文 docstring 第一行写 seam：`"""Seam: python scripts/xxx.py <args> -> stdout JSON"""`，下接输入/输出契约。
- 纯 pytest + `tmp_path`。无 `conftest.py`、无 `pytest.ini`、无 `unittest`、无 `mock`/`monkeypatch`。
- 打桩 = 把 canned JSON 写进 `tmp_path`（伪造 `judges/<metric>.json`、`score.json`、trace）。**不调真实 LLM、不跑 pi CLI**。
- 命名 `test_<行为>`；文件内用 `# --- Slice N: <主题> ---` 分段。
- 同一脚本的新特性批次**追加到新文件**（`test_report2.py`、`test_report3.py`），不改基线文件：基线文件锁契约，批次文件锁回归。
- 要测纯函数（如 `evaluator_fingerprint`）时用 `importlib.util.spec_from_file_location` 加载模块——这是唯一允许的"import 被测脚本"形式。

## ANTI-PATTERNS

- 不 `import` 被测脚本后直调 `main()`：脚本的契约是 CLI + stdout JSON，绕过 seam 就测不到契约（15/15 文件都走 subprocess）。
- 不引入 `conftest.py` / `pytest.ini` / `pyproject.toml` 来共享 fixture——本仓无任何测试配置，`python -m pytest tests` 必须零配置可跑。
- 不让测试真调 LLM 或 pi CLI；`--events` / `--build-only` 就是为此存在的离线 seam。
- 不在测试里依赖或修改 `tests/tmp-ev/`（见 NOTES）。

## NOTES

- `tests/tmp-ev/triggers/should/s1.json`（内容仅 `{"prompt": "x"}`）已被 git 跟踪但全仓零引用，是误提交的 scratch 残渣。
- `test_static_check.py::test_evaluator_self_check_passes` 是自检回归锁：保证 `python scripts/static_check.py skill-evaluator` 输出 `passed: true`（ADR-0012/0013/0015）。
- 该文件是最大测试文件（34 test），因为 #13 扩为五组规则后每组都要正向+负向用例。加规则时必须同时加“不该误报”的负向用例（尤其 `secrets` 的占位符、`obfuscation` 的 warn 分级）。
