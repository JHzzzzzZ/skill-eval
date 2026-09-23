"""Seam: python scripts/judge_runner.py --validate <judge输出.json> -> stdout JSON

LLM 评审（rubric 在 judges/*.md）产出 JSON：{"items": [{name, pass, quote}], "score": 0~1,
"evidence": [...], "reason": str}——score = pass 项数 / 总项数（#5，两位小数），不是 0/1/2 三档。
校验器保证入报告前的结构正确；LLM 调用由主 agent 完成，脚本只做契约校验。
stdout: {"valid": bool, "errors": [...]}
"""
import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "judge_runner.py"
JUDGES_DIR = Path(__file__).parent.parent / "judges"


def run_validate(tmp_path: Path, payload) -> dict:
    p = tmp_path / "judge_out.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--validate", str(p)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, f"script failed: {r.stderr}"
    return json.loads(r.stdout)


# --- Slice 8: 合法输出（#5 逐项计分契约）---

def test_valid_judge_output(tmp_path):
    payload = {"items": [{"name": "有索引结构", "pass": False, "quote": "无"}],
               "score": 0.0, "evidence": ["引用的原文"], "reason": "有部分冗余"}
    assert run_validate(tmp_path, payload)["valid"] is True


def test_score_must_match_items_mapping(tmp_path):
    # #5：score = pass 项数 / 总项数（0~1，两位小数）。score 与 items 不一致即拒绝
    all_pass = {"items": [{"name": "a", "pass": True, "quote": "q1"},
                          {"name": "b", "pass": True, "quote": "q2"}],
                "score": 1.0, "evidence": [], "reason": "全过"}
    assert run_validate(tmp_path, all_pass)["valid"] is True
    partial = {"items": [{"name": "a", "pass": True, "quote": "q1"},
                         {"name": "b", "pass": False, "quote": "q2"},
                         {"name": "c", "pass": False, "quote": "q3"}],
               "score": 0.33, "evidence": ["e"], "reason": "部分"}
    assert run_validate(tmp_path, partial)["valid"] is True
    none_pass = {"items": [{"name": "a", "pass": False, "quote": "q"}],
                 "score": 0.0, "evidence": ["e"], "reason": "全挂"}
    assert run_validate(tmp_path, none_pass)["valid"] is True
    mismatch = {"items": [{"name": "a", "pass": True, "quote": "q"}],
                "score": 0.5, "evidence": ["e"], "reason": "不一致"}
    out = run_validate(tmp_path, mismatch)
    assert out["valid"] is False
    assert any("items" in e for e in out["errors"])


def test_items_structure_required(tmp_path):
    # items 缺失/为空/字段不合法都拒绝
    out = run_validate(tmp_path, {"score": 1.0, "evidence": ["e"], "reason": "x"})
    assert out["valid"] is False and any("items" in e for e in out["errors"])
    out = run_validate(tmp_path, {"items": [], "score": 0.0, "evidence": ["e"], "reason": "x"})
    assert out["valid"] is False
    out = run_validate(tmp_path, {"items": [{"name": "a", "pass": True}], "score": 1.0,
                                  "evidence": ["e"], "reason": "x"})  # 缺 quote
    assert out["valid"] is False
    out = run_validate(tmp_path, {"items": [{"name": "a", "pass": "yes", "quote": "q"}],
                                  "score": 1.0, "evidence": ["e"], "reason": "x"})  # pass 非布尔
    assert out["valid"] is False


# --- Slice 9: 非法输出 ---

def test_score_out_of_range(tmp_path):
    out = run_validate(tmp_path, {"items": [], "score": 1.5, "evidence": [], "reason": "x"})
    assert out["valid"] is False
    assert any("score" in e for e in out["errors"])


def test_evidence_must_be_nonempty_strings(tmp_path):
    # score=2 可以 evidence 为空；score<2 时 evidence 必须非空且全是字符串
    ok = run_validate(tmp_path, {"items": [{"name": "a", "pass": True, "quote": "q"}],
                                 "score": 1.0, "evidence": [], "reason": "无冗余"})
    assert ok["valid"] is True
    bad = run_validate(tmp_path, {"items": [{"name": "a", "pass": False, "quote": "q"}],
                                  "score": 0.5, "evidence": [], "reason": "x"})
    assert bad["valid"] is False
    bad2 = run_validate(tmp_path, {"items": [{"name": "a", "pass": False, "quote": "q"}],
                                   "score": 0.5, "evidence": [123], "reason": "x"})
    assert bad2["valid"] is False


def test_missing_reason(tmp_path):
    out = run_validate(tmp_path, {"items": [{"name": "a", "pass": False, "quote": "q"}],
                                  "score": 0.0, "evidence": ["a"]})
    assert out["valid"] is False
    assert any("reason" in e for e in out["errors"])


def test_malformed_json_file(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not json", encoding="utf-8")
    out = run_validate(tmp_path, {})  # 占位，下面直接跑
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--validate", str(p)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["valid"] is False


# --- Slice 30: 文件缺失受控失败（全仓复审 P2）---

def test_missing_file_controlled(tmp_path):
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--validate", str(tmp_path / "nope.json")],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["valid"] is False
    assert "errors" in out


# --- Slice 31: rubric 文件自身结构（自评发现：契约笔误 / 模板不可解析 / 检查点过少）---

def test_every_rubric_has_machine_readable_contract():
    # 每条 rubric 都要给出可解析的 items 模板 + score 公式 + ≥2 个检查点：模板坏掉时，
    # 主 agent 只能照抄坏形状（judge_runner 直接拒绝）；检查点过少时单项翻转就是 ±0.5 分（ADR-0020）。
    files = sorted(JUDGES_DIR.glob("*.md"))
    assert len(files) >= 6
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert "## 输出（#5 逐项计分契约）" in text, f"{f.name} 缺输出契约小节"
        assert "score = pass 项数 / 总项数" in text, f"{f.name} 缺 score 公式定义"
        blk = re.search(r"```json\n(.*?)\n```", text, re.S)
        assert blk, f"{f.name} 缺 json 输出模板"
        tmpl = json.loads(blk.group(1))          # 模板必须真的是合法 JSON
        assert isinstance(tmpl.get("items"), list) and tmpl["items"], f"{f.name} items 模板为空"
        assert {"name", "pass", "quote"} <= set(tmpl["items"][0]), f"{f.name} item 字段不全"
        assert len(re.findall(r"^\d+\. ", text, re.M)) >= 2, f"{f.name} 检查点少于 2 条"


def test_no_stale_score_placeholder_in_rubrics():
    # 历史笔误：evidence 模板写成“score=2 时可为空数组”，而 #5 契约里没有 2 分档（实际是 score=1）
    for f in sorted(JUDGES_DIR.glob("*.md")):
        assert "score=2" not in f.read_text(encoding="utf-8"), f"{f.name} 残留 score=2 笔误"
