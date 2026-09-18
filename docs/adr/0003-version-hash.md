# 版本号：git commit hash，无 git 时副本上 init 后取 auto-<hash>

同一 skill 多次评估的结果需按版本区分（#14 版本演进）。决定：

1. 被测 skill 自带 `.git` → 版本号 = `git rev-parse --short HEAD`
2. 裸文件夹上传 → **评估器不动用户原始文件**，只读存档到 `uploads/<name>-<时间戳>/`，在**副本**上 `git init + commit`，版本号 = `auto-<副本 commit 短 hash>`
3. `meta.json` 额外记录原始内容的 `content_sha256`，作为 git 丢失后的兜底指纹

原因：用户上传时大概率不是 git 仓库，hash 的作用不是两边对账，而是给评估器自己用——相同内容两次上传得到相同 `auto-<hash>`，报告落同一目录（幂等，#15）；内容变化产生新目录，历史保留（#14）。`auto-` 前缀明确标记该 hash 是评估器侧快照指纹，用户侧无对应 commit。

## Considered Options

- 仅用名字：不反映"改没改"，会覆盖历史结果，毁掉 #14；且不同作者可撞名，污染按名字冻结的评测集
- 自算文件 sha256：无时间顺序语义，不采用（仅作 meta.json 兜底字段）
- 要求上传者自带 git：门槛高，拒绝大量合法上传

## Consequences

- 上传者后来补了 git tag 重新提交 → 新结果目录，旧报告保留
- 名字做 skill 身份（评测集/聚合维度），hash 做版本（结果目录），两层缺一不可
