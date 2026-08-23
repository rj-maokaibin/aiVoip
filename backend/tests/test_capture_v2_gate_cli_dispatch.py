from app.capture_v2 import gate_cli


def test_master_fix_candidate_dispatch_is_exact_sha_and_rc_suffix_bounded(monkeypatch):
    calls = []

    def fake_main(argv):
        calls.append(list(argv))
        return 0

    import app.capture_v2.control.master_fix_candidate_regression as regression
    monkeypatch.setattr(regression, "main", fake_main)

    candidate = gate_cli._MASTER_FIX_CANDIDATE_SHA
    rc = gate_cli._bounded_master_fix_candidate_regression([
        "evaluate", "--bundle", candidate,
        "--gate-id", "MASTER-FIX-CANDIDATE-INTEGRATION-RC63",
    ])

    assert rc == 0
    assert len(calls) == 1
    assert calls[0][-2:] == ["--candidate-sha", candidate]


def test_master_fix_candidate_dispatch_rejects_other_sha_and_unrelated_gate():
    other = "0" * 40
    assert gate_cli._bounded_master_fix_candidate_regression([
        "evaluate", "--bundle", other,
        "--gate-id", "MASTER-FIX-CANDIDATE-INTEGRATION-RC63",
    ]) is None
    assert gate_cli._bounded_master_fix_candidate_regression([
        "evaluate", "--bundle", gate_cli._MASTER_FIX_CANDIDATE_SHA,
        "--gate-id", "MASTER-FIX-CANDIDATE-OTHER-RC63",
    ]) is None
