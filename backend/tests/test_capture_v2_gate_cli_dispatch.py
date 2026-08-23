from pathlib import Path

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


def test_production_preflight_dispatch_requires_exact_authorization_path(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    module = repo / "backend/app/capture_v2/gate_cli.py"
    module.parent.mkdir(parents=True)
    module.write_text("# marker\n")
    authorization = repo / gate_cli._PRODUCTION_AUTH_RELATIVE
    authorization.parent.mkdir(parents=True, exist_ok=True)
    authorization.write_text("{}\n")
    calls = []

    def fake_main(argv):
        calls.append(list(argv))
        return 0

    import app.capture_v2.control.production_deployment_preflight_guarded as preflight
    monkeypatch.setattr(gate_cli, "__file__", str(module))
    monkeypatch.setattr(preflight, "main", fake_main)

    rc = gate_cli._bounded_production_deployment_preflight([
        "evaluate", "--bundle", str(authorization),
        "--gate-id", "PRODUCTION-DEPLOYMENT-PREFLIGHT-RC71",
    ])
    assert rc == 0
    assert calls == [[
        "--repo-root", str(repo),
        "--authorization", str(authorization.resolve()),
    ]]


def test_production_preflight_dispatch_rejects_wrong_bundle_and_gate(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    module = repo / "backend/app/capture_v2/gate_cli.py"
    module.parent.mkdir(parents=True)
    module.write_text("# marker\n")
    monkeypatch.setattr(gate_cli, "__file__", str(module))

    assert gate_cli._bounded_production_deployment_preflight([
        "evaluate", "--bundle", str(repo / "other.json"),
        "--gate-id", "PRODUCTION-DEPLOYMENT-PREFLIGHT-RC71",
    ]) is None
    expected = repo / gate_cli._PRODUCTION_AUTH_RELATIVE
    assert gate_cli._bounded_production_deployment_preflight([
        "evaluate", "--bundle", str(expected),
        "--gate-id", "PRODUCTION-DEPLOYMENT-OTHER-RC71",
    ]) is None
