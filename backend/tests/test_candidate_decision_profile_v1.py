from copy import deepcopy

import pytest
import yaml

from app.analyzers.profile import AnalyzerProfileError, default_analyzer_profile_path, validate_analyzer_profile


def _profile() -> dict:
    return yaml.safe_load(default_analyzer_profile_path().read_text(encoding="utf-8"))


def test_candidate_decision_profile_v13_is_valid():
    raw = _profile()
    assert raw["version"] == "1.3.0"
    validate_analyzer_profile(raw)


def test_candidate_decision_ratio_above_one_is_rejected():
    raw = deepcopy(_profile())
    raw["candidate_decision"]["silence_counterpart_active_ratio"] = 1.1
    with pytest.raises(AnalyzerProfileError, match="ANALYZER_PROFILE_OUT_OF_RANGE"):
        validate_analyzer_profile(raw)


def test_candidate_decision_negative_guard_is_rejected():
    raw = deepcopy(_profile())
    raw["candidate_decision"]["dtmf_guard_ms"] = -1
    with pytest.raises(AnalyzerProfileError, match="ANALYZER_PROFILE_OUT_OF_RANGE"):
        validate_analyzer_profile(raw)
