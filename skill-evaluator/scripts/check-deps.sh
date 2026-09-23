#!/usr/bin/env bash
# 前置自检（#16）：确认评估器能在这台机器上跑起来。
# 用法：bash scripts/check-deps.sh    （退出码 0 = 可跑；1 = 缺硬依赖）
# 硬依赖缺失 → FAIL；可选能力缺失（pi CLI / 模型变量）→ warn，不阻断静态与 LLM 评审。
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
FAIL=0
ok()   { printf "  [ok]   %s\n" "$1"; }
bad()  { printf "  [FAIL] %s\n" "$1"; FAIL=1; }
warn() { printf "  [warn] %s\n" "$1"; }

echo "skill-evaluator 依赖自检（python3 >= 3.9 / git / pytest / pi CLI）"

# --- python3 >= 3.9 ---
# 注意：Windows 上 `python3` 是应用商店占位符，能 command -v 找到但跑不起来 → 必须实跑验证再采纳。
PY=""
PYVER=""
for c in python3 python py; do
  command -v "$c" >/dev/null 2>&1 || continue
  v="$("$c" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)" || continue
  [ -n "$v" ] || continue
  PY="$c"; PYVER="$v"; break
done
if [ -z "$PY" ]; then
  bad "找不到可用的 python3 / python"
elif "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
  ok "python $PYVER ($PY)"
else
  bad "python $PYVER 低于 3.9（Path.is_relative_to 需要）"
fi

# --- git（沙箱 = git worktree，ADR-0001）---
if command -v git >/dev/null 2>&1; then
  ok "git $(git --version | awk '{print $3}')"
else
  bad "找不到 git（沙箱运行需要 git worktree）"
fi

# --- pytest（仅测试用）---
if [ -n "$PY" ] && "$PY" -m pytest --version >/dev/null 2>&1; then
  ok "pytest $("$PY" -m pytest --version 2>/dev/null | awk '{print $2}')"
else
  warn "pytest 未装：pip install -r requirements.txt（仅跑 tests 需要）"
fi

# --- 包内文件齐备（#16「声明与实现一致」：正文说“包内文件齐备”，就得真查齐）---
# 为什么用显式清单而不是 `for f in "$ROOT"/scripts/*.py`：glob 只会展开成**已存在**的文件，
# 删掉任何一个都拓不到（实测：删 report.py + contract.md 仍退出 0）。清单 = 流程声明要用的东西。
REQUIRED="SKILL.md reference.md requirements.txt LICENSE scripts/check-deps.sh
scripts/static_check.py scripts/evalset_count.py scripts/evalset_check.py scripts/trace_run.py
scripts/trigger_judge.py scripts/judge_runner.py scripts/score.py scripts/idem.py scripts/compare.py
scripts/process.py scripts/report.py scripts/evolution.py scripts/model_robust.py scripts/ablation.py
judges/brevity.md judges/redundancy.md judges/fallback.md judges/precheck.md judges/contract.md
judges/side-effects.md judges/deps.md judges/trigger.md"
MISSING=0
for f in $REQUIRED; do
  if [ -f "$ROOT/$f" ]; then :; else bad "缺 $ROOT/$f（评估器自身损坏）"; MISSING=1; fi
done
if [ "$MISSING" -eq 0 ]; then
  ok "流程所需文件齐备（$(printf '%s\n' $REQUIRED | wc -l | tr -d ' ') 个）"
fi

# --- 可选：pi CLI 与模型（沙箱运行 + LLM 评审用；ADR-0007/0009）---
if command -v pi >/dev/null 2>&1 || command -v pi.cmd >/dev/null 2>&1; then
  ok "pi CLI（沙箱运行与 LLM 评审可用）"
else
  warn "找不到 pi CLI：只能跑静态检查，沙箱类指标将 skipped"
fi
if [ -n "${SKILL_EVAL_MODEL:-}" ]; then
  ok "SKILL_EVAL_MODEL=$SKILL_EVAL_MODEL"
else
  warn "未设 SKILL_EVAL_MODEL：用 pi 默认模型（ADR-0009）"
fi

echo ""
if [ "$FAIL" -eq 0 ]; then
  echo "自检通过：可以开始评估。"
else
  echo "自检失败：先补齐上面 [FAIL] 项，不要继续跑（否则指标会静默降级）。"
fi
exit "$FAIL"
