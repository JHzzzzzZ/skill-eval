"""校准统计的纯函数（不调 LLM）。"""
import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "calibrate.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("calibrate", SCRIPT)
cal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cal)


def test_majority_and_tie():
    assert cal._majority([True, True, False]) == (True, 2 / 3)
    assert cal._majority([True, False]) == (False, 0.5)
    assert cal._majority([None, None]) == (None, 0.0)


def test_calibration_cases_are_well_formed():
    root = Path(__file__).parent.parent / "calibration"
    expected_names = {
        "brevity": {"无可下沉细节", "有索引结构"},
        "precheck": {"校验可照做", "校验置前", "依赖覆盖完整",
                     "声明与实现一致", "失败处置明确", "分级正确"},
    }
    for metric, names in expected_names.items():
        cases = sorted((root / metric).glob("*/expected.json"))
        assert len(cases) >= 8, metric
        for path in cases:
            case = path.parent
            assert (case / "skill" / "SKILL.md").is_file(), case
            data = json.loads(path.read_text(encoding="utf-8"))
            assert set(data.get("items", {})) == names, case


def test_summarize_agreement_and_accuracy():
    out = cal.summarize([
        {"expected": {"a": True, "b": False},
         "observations": {"a": [True, True, False], "b": [False, False, True]}},
    ], 3)
    assert out["items"]["a"]["agreement"] == round(2 / 3, 3)
    assert out["items"]["a"]["accuracy"] == 1.0
    assert out["items"]["b"]["agreement"] == round(2 / 3, 3)
    assert out["items"]["b"]["accuracy"] == 1.0
    assert out["overall_agreement"] == round(2 / 3, 3)
