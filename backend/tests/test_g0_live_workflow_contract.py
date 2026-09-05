from pathlib import Path


def test_g0_live_gate_repairs_workspace_before_checkout_and_keeps_authority() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github/workflows/golden-cfg-config-live.yml").read_text(encoding="utf-8")

    repair = "- name: Repair self-hosted workspace before checkout"
    checkout = "- name: Checkout exact master"
    authorize = "- name: Authorize exact master and explicit live mutation"
    execute = "- name: Execute Golden CFG CONFIG 001 with mandatory restore"

    assert repair in workflow
    assert checkout in workflow
    assert authorize in workflow
    assert execute in workflow
    assert workflow.index(repair) < workflow.index(checkout) < workflow.index(authorize) < workflow.index(execute)

    assert "G0_RUNNER_WORKSPACE_REPAIR=PASS" in workflow
    assert 'expected="/run-golden-cfg-config ${head_sha}"' in workflow
    assert 'test "$TRIGGER_ACTOR" = "$GITHUB_REPOSITORY_OWNER"' in workflow
    assert 'test "$TRIGGER_BODY" = "$expected"' in workflow
    assert "--allow-live-mutation" in workflow
    assert "Remove runtime secrets" in workflow
