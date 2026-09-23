# 评估器指纹：内容 sha256，落 report.json 与 results/meta.json

#14 版本演进只对比被测 skill 的不同版本，但评估器本身也在改。报告若不记录"由哪个评估器产生"，跨版本指标差异会被误读成被测 skill 的回归。决定：

1. 评估器指纹 = `scripts/*.py` + `judges/*.md` + `SKILL.md` + `reference.md` 的内容 sha256（相对路径也参与），版本号取前 8 位，形如 `auto-3f9c1a7b`
2. 指纹写进 `report.json` 顶层 `evaluator` 与 `results/<version>/meta.json`
3. `results/meta.json` 由 report.py 写入，带 `schema: skill-eval/results-meta/1`；**已存在则合并保留人工填写的键**（`model`/`sandbox`/`skipped_reason` 等评估器重建不出来的记录）——评估器字段一律用自己的键名：`evaluator`、`skill_fingerprint`、`evalset_source`，避开人工已经用了的 `skill`/`evalset`。另外两个同名文件（uploads 副本的 `content_sha256`、`evalsets/<name>/vN/meta.json` 冻结标记）不改名，靠 schema 字段区分
4. `report.py --skill <被测skill目录>` 可选：把被测 skill 的 `content_sha256` 一并写进 meta.json（ADR-0003 的兜底指纹落地——vN 目录命名下这是唯一能看出"评的是哪份内容"的来源）
5. evolution.py 读各版本指纹：不一致 → `comparable=false` 并说明"不构成回归证据"；部分缺失 → `comparable=null`（旧报告，可比性未知）

原因：人手维护的 semver 需要记得改，忘了就骗人——本仓库三处 meta.json 全部只在文档里存在、代码一个都没写，就是纪律不可靠的证据。内容指纹零维护、无法忘记更新、同内容可复现。

## Considered Options

- 人手 semver（如 1.2.0）：人可读、能表达 breaking change，但依赖纪律，漏改即失真
- 用 git commit/tag 做指纹：评估器目录不保证是 git 仓库，工作区未提交的改动也不会反映
- 改掉三处同名 meta.json：三者在三个不同目录、用途不同，改名成本高于收益

## Consequences

- 评估器任何一次改动都会让历史报告标成"不可比"——这是想要的：宁可提示不可比，也不要静默误读
- 单看 results 目录即可自证：评估器与被测 skill 各是哪份内容
- `meta.json` 可能出现人工字段与评估器字段共存（如人工的 `skill: "code-review"` 与评估器的 `skill_fingerprint: {...}`）——这是有意的：评估器不删自己读不懂的记录
