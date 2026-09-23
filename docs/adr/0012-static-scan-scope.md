# #13 静态扫描口径：默认排除夹具与痕迹目录，抑制命中须显式标记

评估器过不了自己的 #13：`static_check.py` 扫 `tests/` 夹具（按构造写满 `rm -rf`）、`evalsets/` 历史 trace、以及扫描器自身的正则表定义行（`\bsudo\b` 匹配自己的定义行），ADR-0008 的硬编码闸门同样打在 reference.md 的说明行上。实测 `python scripts/static_check.py skill-evaluator` → 5 处危险命令 + 106 处宿主硬编码，自检必然 fail，报告里的 #13 是假阳性，与 ADR-0006 自闭环直接冲突。

决定（三条口径，排除与抑制结果都必须在 stdout 可见）：

1. 默认排除目录名 `tests/`、`evalsets/`，以及扫描器自身源码（`<skill>/scripts/<自身文件名>`）；`--no-default-excludes` 关闭默认排除，用于审计模式
2. `--exclude <路径>` 的相对路径按**被测 skill 目录**解析（不是 CWD）；绝对路径原样（旧实现按 CWD 解析，从别处调用时排除静默失效）
3. 行内含 `static-check:ignore` → 该行命中不计入 `errors`，改记 `ignored` 字段（文档里合法引用危险模式时用）

#13 的边界是"skill 会被 agent 执行的命令面"（reference.md § 权限边界），夹具与历史 trace 不属于执行面。被拒绝的方案：夹具也计入 fail（评估器永久自失败，除非删测试）；只靠调用方逐条 `--exclude`（排除清单随复制/换机丢失，SKILL.md 无法自闭环）；改写正则表以避免自匹配（如 `r"\bsud" + r"o\b"`，用可读性换 1 行，且下一个模式还会再犯）。

## Consequences

- 自检 `python scripts/static_check.py skill-evaluator` → `passed: True`；`test_evaluator_self_check_passes` 锁住该回归
- stdout 契约新增 `excluded` / `ignored` 两个字段（原字段不变）。report.py 的 #13 条目**暂未**回显这两个字段，眼下靠读 `static.json`
- 扫描面确实变小：把危险命令藏进 `tests/` 的 skill 会漏判 → 全量扫描用 `--no-default-excludes`
- 抑制标记可被恶意 skill 用来隐藏真实危险命令；#13 本就不做强制拦截（reference.md § 权限边界），兜底是 trace 审计
