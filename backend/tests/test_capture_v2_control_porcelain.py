from app.capture_v2.control.policy import ControlPolicy


def test_porcelain_path_accepts_one_and_two_column_forms():
    assert ControlPolicy._porcelain_path("M validation/control/status.json") == "validation/control/status.json"
    assert ControlPolicy._porcelain_path(" M validation/control/status.json") == "validation/control/status.json"
    assert ControlPolicy._porcelain_path("M  validation/control/status.json") == "validation/control/status.json"
    assert ControlPolicy._porcelain_path("?? validation/control/results/x/result.json") == "validation/control/results/x/result.json"
