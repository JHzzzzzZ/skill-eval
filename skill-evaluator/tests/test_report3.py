"""data 渲染层：表格优先 + 原始 JSON 折叠 + 不留静默截断。

背景：#15 的 data 2165 字符，`report.html` 旧实现 `json.dumps(...)[:2000]` 把它切成非法 JSON，
而 report.md 那边是一整坨 2165 字的 JSON；两边都没有测试覆盖。约定（见 reference.md § data 渲染）：
- 已知形状（#15/#12/#4/#9）→ 摘要行 + 表格；原始 JSON 在 HTML 里折叠保留
- 无表可渲染：≤400 字符内联，>400 折叠；report.md 侧 >400 只留「完整 data 见 report.json」指针
- 单元格 >800 字符截断并标注，全文仍在折叠 JSON 里
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "report.py"


def write_inputs(d: Path, files: dict):
    for name, data in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def long_idem(pairs: int = 15) -> dict:
    """15 对相邻跑（run3 实测形状），末尾带标记串用于检测头截断。"""
    per = [{"case": f"c{(i % 5) + 1}", "pair": f"golden-c{(i % 5) + 1}->repeat-c{(i % 5) + 1}-{(i % 3) + 1}",
            "ratio": round(0.09 * (i + 1), 4), "repeated_steps": i, "total_steps": 11 + i,
            "idempotent": True} for i in range(pairs - 1)]
    per.append({"case": "c5", "pair": "TAIL-MARKER->repeat-c5-3", "ratio": 0.0,
                "repeated_steps": 0, "total_steps": 11, "idempotent": True})
    return {"ratio": 0.1478, "idempotent": True, "pairs": pairs, "idempotent_pairs": pairs, "per_pair": per}


def run_html_md(d: Path):
    r = subprocess.run([sys.executable, str(SCRIPT), str(d), "--html"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    return ((d / "report.html").read_text(encoding="utf-8"),
            (d / "report.md").read_text(encoding="utf-8"))


def base_inputs(idem=None, compare=None, process=None, brevity=1) -> dict:
    cost = {"tool_calls": {"mean": 10.8, "std": 6.99, "n": 20, "min": 3, "max": 33},
            "tokens": {"mean": 42195.1, "std": 12498.9, "n": 20, "min": 29848, "max": 83385},
            "seconds": {"mean": 96.3, "std": 67.5, "n": 20, "min": 36.4, "max": 315.6}}
    files = {"static.json": {"name": "s", "description": "d", "description_tokens": 226,
                             "invoke": {"resolved": "both"}, "errors": [], "warnings": [],
                             "dangerous": [], "passed": True},
             "score.json": {"trigger": {"precision": 0.53, "recall": 1.0, "f1": 0.69}, "cost": cost,
                            "cost_by_case": {"c1": {"tool_calls": {"mean": 15.0, "std": 6.82, "n": 4}},
                                             "c2": {"tool_calls": {"mean": 14.2, "std": 10.89, "n": 4}}},
                            "necessity": "skipped", "passed": True}}
    files["judges/brevity.json"] = {"items": [{"name": f"检查点{i}", "pass": True, "quote": f"引文{i}"}
                                               for i in range(brevity)],
                                    "score": 1.0, "evidence": [],
                                    "reason": "正文 70 行，无可下沉细节（judge reason 应出现在 HTML 摘要行）"}
    if idem is not None:
        files["idem.json"] = idem
    if compare is True:
        files["compare.json"] = {"mean_score": 2.0, "match": True, "note": "5/5 case 匹配",
                                 "per_case": [{"name": f"c{i}", "match": True, "score": 2,
                                               "reason": f"case {i} 的理由" * 1} for i in range(1, 6)]}
    if process is not None:
        files["process.json"] = process
    return files


def test_long_data_not_truncated_in_html(tmp_path):
    # 回归：旧实现 [:2000] 会把 2165 字符的 #15 data 切成非法 JSON（尾部的 TAIL-MARKER 丢失）
    write_inputs(tmp_path, base_inputs(idem=long_idem()))
    html, _md = run_html_md(tmp_path)
    assert "TAIL-MARKER" in html                      # 尾部内容活着（没被 2000 截断）
    assert "<details" in html and "per_pair" in html  # 原始 data 折叠保留（report.json 同源，HTML 里引号被转义）
    assert html.count("<table") >= 1
    raw = json.dumps(long_idem(), ensure_ascii=False, indent=2)
    assert len(raw) > 2000                             # 前提：这条 data 确实超过旧阈值


def test_md_tabulated_and_long_untabulated_gets_pointer(tmp_path):
    write_inputs(tmp_path, base_inputs(idem=long_idem(),
                                       process={"meaningless": ["越界步骤" * 200], "score": 0,
                                                "reason": "过程与声明不符"}))
    html, md = run_html_md(tmp_path)
    # #15：Markdown 表格 + 摘要行，不再重复整块 JSON
    idem_sec = md.split("### #15")[1].split("### ")[0]
    assert "| case | pair | ratio |" in idem_sec and "ratio 0.148" in idem_sec  # _fmt 保留 3 位
    assert "```json" not in idem_sec
    # #10：无表可渲染且 >400 字符 → 只留指针，不给一坨 JSON
    proc_sec = md.split("### #10")[1].split("### ")[0]
    assert "完整 data 见 `report.json`" in proc_sec and "```json" not in proc_sec
    # 短 data（#2 只有 description_tokens）仍内联
    assert "```json" in md.split("### #2")[1].split("### ")[0]
    # HTML 侧同样保留（折叠）
    assert "<details" in html


def test_judge_metrics_show_summary_and_raw(tmp_path):
    # 旧 HTML 只渲染 items，丢了 score/reason/evidence；#6 口径要求 html 不得比 md 少信息
    write_inputs(tmp_path, base_inputs(brevity=3))
    html, md = run_html_md(tmp_path)
    assert "正文 70 行，无可下沉细节" in html          # 摘要行含 judge reason
    assert "检查点" in html and "<table" in html
    assert "evidence" in html                          # 原始 judge data 可见（内联或折叠）
    assert "| 检查点 | 结果 | 原文引用 |" in md


def test_long_cell_truncated_but_full_text_in_folded_json(tmp_path):
    long_reason = "理由" + "细节" * 400 + "尾部标记TAIL"
    write_inputs(tmp_path, {"compare.json": {"mean_score": 1.0, "match": True, "note": "1/1 匹配",
                                             "per_case": [{"name": "c1", "match": True, "score": 1,
                                                           "reason": long_reason}]}})
    html, _md = run_html_md(tmp_path)
    assert "…（截断，全文见原始 data）" in html        # 单元格截断并标注
    assert "尾部标记TAIL" in html                      # 全文仍在折叠的原始 data 里
    assert len(long_reason) > 800                     # 前提：确实超过单元格阈值
