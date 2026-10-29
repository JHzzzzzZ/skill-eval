# 长度阈值改为字符/行，并放宽 human 型 frontmatter

需求 2 要给 description 与正文分警告/失败两档；需求 1 说 human 型可以没有 name/description，
也不需要 T2A 精准触发。原实现是 description 约 100 token 仅警告、正文 >150 行仅警告，
且缺 name/description 直接 error，和 human 型冲突。

决定：

1. description 按 **Unicode 字符数**（`len(description.strip())`）：>384 warning，>1024 fail；
   不再用 100-token 估算。
2. 正文按行（frontmatter 之后全文，含空行/代码块）：>150 warning，>500 fail；不再只警告。
3. name 保留 >64 字符 warning；human 型缺 name/description 不记 error。
4. human 判定 = `disable-model-invocation: true` 或 name/description 同时缺失；两者缺失时
   新 #1（name/description 规范）记 skipped；存在则照常按 384/1024 判；human 一律跳过
   T2A（新 #10）。
5. `static.json` 新增 `description_chars` / `description_warn_chars` / `description_fail_chars` /
   `description_level`、`skill_md_body_warn_lines` / `skill_md_body_fail_lines` / `body_level`；
   `report.py` 优先读结构化字段，旧 `static.json` 回退旧 token 逻辑。

原因：为什么用字符不用 token——字符数是可复现的静态事实，不依赖分词器；为什么不用真实
tokenizer——会引入第三方依赖，且中英文混排的 token 数随模型变化；为什么 human 型放宽——它的
调用者是读文档的人，description 不参与模型触发，缺它不是缺陷。代价：同样 1024 字符，中文的
信息量约等于英文的数倍，阈值对中文更宽；旧 `static.json` 的 token 口径要保留一段时间做兼容。

## Considered Options

- 按 token 估算：沿用旧实现，但 384/1024 的口径会随分词器漂移，且用户明确要字符。
- human 型仍要求 name/description：与“human 可以没有”直接冲突。

## Consequences

- `static_check.py` 的 `DESCRIPTION_TOKEN_LIMIT` / `SKILL_MD_BODY_MAX_LINES` 退役，改读 config。
- `report.py` 的 #1/#2 裁决从“子串匹配 errors/warnings”改成读结构化字段；旧产物走回退分支。
- 报告 note 要写清“字符”还是“行”，不能让读者以为是 token。
