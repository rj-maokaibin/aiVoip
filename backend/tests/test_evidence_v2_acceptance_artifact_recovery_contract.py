from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "deploy" / "production_deploy_wrapper.sh"


def test_acceptance_artifact_is_recovered_before_original_failure_is_returned() -> None:
    text = WRAPPER.read_text(encoding="utf-8")

    required = [
        "recover_evidence_v2_acceptance()",
        "NO_CURRENT_ATTEMPT_RESULT",
        "stat -c '%Y'",
        "EVIDENCE_V2_ACCEPTANCE_ARTIFACT_RECOVERY=PASS",
        'payload.get("contract") == "evidence-v2-production-rollout-acceptance-v1"',
        'payload.get("source_revision") == target',
        'deploy_started_at="$(date +%s)"',
        "set +e",
        'deploy_rc="$?"',
        'recover_evidence_v2_acceptance "$deploy_started_at"',
        'exit "$deploy_rc"',
    ]
    missing = [marker for marker in required if marker not in text]
    assert not missing, missing

    deploy_pos = text.index('./deploy/voip-ai --env "$ENV_FILE" --revision "$TARGET" deploy')
    recover_pos = text.index('recover_evidence_v2_acceptance "$deploy_started_at"')
    exit_pos = text.index('exit "$deploy_rc"')
    assert deploy_pos < recover_pos < exit_pos
