from pathlib import Path

from tools.ai_eval_gate import REQUIRED, evaluate


def test_ai_eval_manifest_covers_spec_categories_and_hard_zero_contract():
    root=Path(__file__).resolve().parents[2]
    result=evaluate(root/"golden_cases"/"ai_shadow_eval_v1.json")
    assert result["status"]=="PASS"
    assert result["category_count"]==len(REQUIRED)==19
    assert result["real_history_required"] is True
