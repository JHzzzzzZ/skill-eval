"""报告页配色回归（同色底 + 同色字 = 看不见）。

Seam: python scripts/report.py <results目录> --html -> report.html

BUG（本次修复）：`_badge()` 发出 `class='badge vf-pass'`（没有裸 `.vf`），
于是 `.vf.badge{color:#fff}` 永不命中，而 `.vf-pass{color:#0b7a3d}` 命中 →
绿字压在同色底 `.badge.vf-pass{background:#0b7a3d}` 上，四档徽章全部隐形。
本文件用 CSS 级联求解器 + 对比度检查锁住"页面上任何带底色的裁决标记必须可读"，
不靠"某条 CSS 规则长什么样"这种易碎的字符串断言。
覆盖：1 徽章四档对比度 / 2 无底色的独立符号（vf vf-x）对比度 / 3 修复前形态必须判失败。
"""
import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "report.py"
JUDGES = ("brevity", "redundancy", "fallback", "precheck", "contract",
          "side-effects", "deps", "pii")
# 徽章四档的底色（与 _HTML_TMPL 的 .badge.vf-* 同源，仅用于测试断言）
BADGE_BG = {"pass": "#0b7a3d", "warn": "#b7791f", "fail": "#b23b3b", "skipped": "#6b7280"}
BODY_COLOR = "#1c2733"   # body{color:...}：徽章没自带颜色时的继承值
CARD_BG = "#ffffff"      # .card{background:#fff}：卡片内无底色元素的实际背景
MIN_CONTRAST = 3.0       # WCAG 非文本/图形元素下限


# --- CSS 级联求解（只认简单选择器：元素名 + 任意多个 .class，无组合子/伪类/属性）---

def style_block(html: str) -> str:
    m = re.search(r"<style>(.*?)</style>", html, re.S)
    assert m, "报告页里找不到 <style> 块"
    return m.group(1)


def _rules(css: str):
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)   # 注释会污染选择器片段（本次就踩过）
    for sel_group, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        decls = {}
        for d in body.split(";"):
            if ":" in d:
                k, v = d.split(":", 1)
                k, v = k.strip(), v.strip()
                # 报告页大量用 `background:` 简写；只在值是纯色时折算成 background-color
                if k == "background" and re.fullmatch(r"#[0-9a-fA-F]{3,6}", v):
                    decls["background-color"] = v
                    continue
                decls[k] = v
        for sel in sel_group.split(","):
            sel = sel.strip()
            if not sel or any(c in sel for c in " >+~[:#"):
                continue   # 组合子/伪类/属性/ID：本页的裁决标记用不到，跳过不猜
            if sel.startswith("*"):
                yield "", [], 0, decls
                continue
            classes = re.findall(r"\.([\w-]+)", sel)
            tag = re.match(r"^[a-zA-Z][\w-]*", sel)
            if len(sel.replace(".", "")) != len("".join(classes)) + (len(tag.group(0)) if tag else 0):
                continue   # 有无法解析的残留 → 跳过
            # 特异性：(类数, 元素数)，同特异度则源码顺序靠后者胜
            yield (tag.group(0).lower() if tag else ""), classes, len(classes) * 10 + (10 if tag else 0), decls


def resolve(css: str, tag: str, classes, inherited_color=BODY_COLOR, inherited_bg=CARD_BG):
    """返回 (color, background-color)：只按该元素自身命中的声明求解，其余用继承/容器默认。"""
    props = {"color": inherited_color, "background-color": inherited_bg}
    best = {}
    for i, (r_tag, r_classes, spec, decls) in enumerate(_rules(css)):
        if r_tag and r_tag != tag:
            continue
        if any(c not in classes for c in r_classes):
            continue
        for k in ("color", "background-color"):
            if k in decls and (k not in best or spec >= best[k][0]):
                best[k] = (spec, decls[k])
    for k, (_spec, v) in best.items():
        props[k] = v
    return props


def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    chans = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        chans.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * chans[0] + 0.7152 * chans[1] + 0.0722 * chans[2]


def contrast(c1: str, c2: str) -> float:
    a, b = _luminance(c1), _luminance(c2)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


# --- 夹具：一份能同时产出 pass/warn/fail/skipped 的最小输入（同 test_report4 的 partial_inputs）---

def judge(score=1.0, items=None) -> dict:
    items = items if items is not None else [{"name": "检查点", "pass": True, "quote": "引文"}]
    return {"items": items, "score": score, "evidence": [], "reason": "评审理由"}


def mixed_inputs() -> dict:
    files = {
        "static.json": {"name": "s", "description": "d", "description_tokens": 50,
                        "invoke": {"allowed": ["both", "human"], "resolved": "both", "note": ""},
                        "dangerous": [], "secrets": [], "injection": [], "exfil": [],
                        "obfuscation": [], "pii": [], "hardcoded": [], "stale_refs": [],
                        "excluded": [], "ignored": [], "scan_excluded_dirs": [],
                        "skill_md_body_lines": 70, "skill_md_body_max_lines": 150,
                        "errors": [], "warnings": [], "issues": [], "passed": True},
        "score.json": {
            "trigger": {"precision": 1.0, "recall": 1.0, "f1": 1.0},
            "cost": {k: {"mean": 10, "std": 0, "n": 3, "min": 10, "max": 10}
                     for k in ("tool_calls", "tokens", "seconds")},
            "cost_by_case": {"c1": {"tool_calls": {"mean": 10, "std": 0, "n": 2, "min": 10, "max": 10}}},
            "necessity": {k: {"golden": 8, "baseline": 10, "improvement": 0.2}
                          for k in ("tokens", "tool_calls", "seconds")},
            "passed": True},
        "golden.json": {"steps": [], "answer": "a", "errors": [], "tokens": 10, "seconds": 1},
        "compare.json": {"mean_score": 2.0, "match": True, "note": "2/2 匹配",
                         "per_case": [{"name": "c1", "match": True, "score": 2, "reason": "对"}]},
        "process.json": {"score": 2, "meaningless": [], "reason": "过程与声明一致"},
        "idem.json": {"ratio": 0.1, "idempotent": True, "pairs": 3, "idempotent_pairs": 3,
                      "per_pair": [{"case": "c1", "pair": "a->b", "ratio": 0.1,
                                    "repeated_steps": 1, "total_steps": 10, "idempotent": True}]},
        "evolution.json": {"versions": ["v1"], "changes": [], "comparable": True,
                           "latest_conclusion": "all-pass"},
    }
    for name in JUDGES:
        files[f"judges/{name}.json"] = judge()
    # 混合裁决：fail（#13 可移植性闸门）+ warn（#11 部分过）+ skipped（#8 无 golden）
    files["static.json"]["hardcoded"] = [{"pattern": "posix", "line": 7, "file": "SKILL.md",
                                          "text": "python /Users/someone/x.py"}]
    files["judges/fallback.json"] = judge(0.5, [
        {"name": "有降级声明", "pass": False, "quote": "未找到降级声明"},
        {"name": "失败可恢复", "pass": True, "quote": "重试 3 次"}])
    del files["golden.json"]
    return files


def render(tmp_path: Path):
    d = tmp_path / "results"
    d.mkdir(exist_ok=True)
    for name, data in mixed_inputs().items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    r = subprocess.run([sys.executable, str(SCRIPT), str(d), "--html"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    html = (d / "report.html").read_text(encoding="utf-8")
    return json.loads((d / "report.json").read_text(encoding="utf-8")), html


def test_fixture_has_all_four_verdicts(tmp_path):
    """夹具本身要覆盖四档，否则下面的对比度检查会漏档（fail fast）。"""
    rep, html = render(tmp_path)
    verdicts = {v["verdict"] for v in rep["metrics"].values()}
    assert verdicts == {"pass", "warn", "fail", "skipped"}, verdicts
    for v in BADGE_BG:
        assert f"class='badge vf-{v}'" in html, f"页面里没有 {v} 徽章，夹具失效"


# --- Slice 1：徽章（同色底 + 白字）必须可读 ---

def test_verdict_badges_are_readable(tmp_path):
    """四档徽章的最终文字色 vs 底色对比度 ≥ 3.0（修复前是绿字压绿底，对比度 = 1.0）。"""
    _rep, html = render(tmp_path)
    css = style_block(html)
    bad = []
    for verdict, bg in BADGE_BG.items():
        props = resolve(css, "span", {"badge", f"vf-{verdict}"})
        assert props["background-color"] == bg, (
            f"{verdict} 徽章底色变了：CSS 求解得 {props['background-color']}，"
            f"测试常量写的是 {bg}——同步 BADGE_BG 后重跑")
        c = contrast(props["color"], props["background-color"])
        if c < MIN_CONTRAST:
            bad.append(f"vf-{verdict}: color={props['color']} bg={props['background-color']} contrast={c:.2f}")
    assert bad == [], "以下徽章的文字与底色接近到看不见：\n" + "\n".join(bad)


def test_badge_text_is_light_on_colored_background(tmp_path):
    """徽章文字必须是浅色：深色文字 + 深色底是本次 BUG 的形态。"""
    _rep, html = render(tmp_path)
    css = style_block(html)
    for verdict in BADGE_BG:
        props = resolve(css, "span", {"badge", f"vf-{verdict}"})
        assert _luminance(props["color"]) > _luminance(props["background-color"]), (
            f"vf-{verdict} 徽章的文字（{props['color']}）不比底色（{props['background-color']}）亮")


def test_badges_keep_verdict_classes(tmp_path):
    """修 CSS 不得动契约：徽章仍是 `badge vf-<裁决>`，glyph + 原文都在（ADR-0030）。"""
    _rep, html = render(tmp_path)
    for verdict, glyph in (("pass", "✓"), ("warn", "⚠"), ("fail", "✗"), ("skipped", "–")):
        m = re.search(rf"class='badge vf-{verdict}'>(.*?)</span>", html)
        assert m, f"缺 {verdict} 徽章"
        assert m.group(1).startswith(glyph), m.group(1)
        assert verdict in m.group(1), m.group(1)


# --- Slice 2：没有底色的独立符号（vf vf-x）在卡片白底上必须可读 ---

def test_standalone_glyphs_readable_on_card(tmp_path):
    """总览表/检查点表里的 ✓/✗ 只有字形色、无底色 → 与卡片白底比对。"""
    _rep, html = render(tmp_path)
    css = style_block(html)
    bad = []
    for verdict in BADGE_BG:
        props = resolve(css, "span", {"vf", f"vf-{verdict}"})
        c = contrast(props["color"], props["background-color"])
        if c < MIN_CONTRAST:
            bad.append(f"vf-{verdict}: color={props['color']} bg={props['background-color']} contrast={c:.2f}")
    assert bad == [], "以下结果符号在白底上读不清：\n" + "\n".join(bad)


# --- Slice 3：修复前的形态必须被判失败（证明检查器真的会红）---

def test_checker_catches_same_color_text_and_background():
    """直接把 BUG 形态喂给求解器：`.badge.vf-pass` 同色 + `.vf-pass` 同色 → 对比度 1.0。"""
    buggy = (".badge.vf-pass{background:#0b7a3d}"
             ".vf-pass{color:#0b7a3d}")
    props = resolve(buggy, "span", {"badge", "vf-pass"})
    assert props["color"] == props["background-color"] == "#0b7a3d"
    assert contrast(props["color"], props["background-color"]) < MIN_CONTRAST
