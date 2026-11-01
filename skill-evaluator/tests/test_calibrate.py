"""校准统计的纯函数（不调 LLM）。"""
import importlib.util
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
