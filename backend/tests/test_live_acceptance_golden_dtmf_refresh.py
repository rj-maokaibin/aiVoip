from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_live_golden_refresh_is_exact_sha_content_gated_and_idempotent():
    live = (ROOT / "tools/human_evidence_feishu_live_acceptance.py").read_text(encoding="utf-8")
    assert "REAL_GOLDEN_001_SHA256" in live
    assert "_golden_evidence" in live
    assert "_dtmf_source_readiness" in live
    assert "_refresh_stale_golden_dtmf" in live
    assert 'GOLDEN_DTMF_DIGITS = "601"' in live
    assert '"DTMF_INSPECTOR"' in live
    assert "DTMF_SIP_DIAL_MATCH" in live
    assert "PCM_601_ACCEPTED_EVENT_MISSING" in live
    assert "MEDIA_601_SIP_MATCH_MISSING" in live
    assert "MEDIA_601_PCM_WAV_MISSING" in live
    assert "DTMF_SCOPE_COHERENCE_MISSING" in live
    assert "GOLDEN_DTMF_SOURCE_NOT_READY" in live
    assert 'if not before["pcm_ready"]' in live
    assert 'if not intermediate["media_ready"] or not intermediate["scope_coherent"]' in live


def test_live_golden_refresh_uses_formal_analyzer_jobs_without_background_notifications():
    live = (ROOT / "tools/human_evidence_feishu_live_acceptance.py").read_text(encoding="utf-8")
    pcm_worker = (ROOT / "backend/app/workers/pcm_tasks.py").read_text(encoding="utf-8")
    media_worker = (ROOT / "backend/app/workers/media_tasks.py").read_text(encoding="utf-8")

    assert "create_pcm_analysis_job" in live
    assert "create_media_analysis_job" in live
    assert "analyze_pcm_evidence.run" in live
    assert "analyze_media_evidence.run" in live
    assert "GOLDEN_PCM_PROFILE_ID, False" in live
    assert "def analyze_pcm_evidence(self, job_id: str, evidence_id: str, profile_id: str, notify: bool = True):" in pcm_worker
    assert "def analyze_media_evidence(self, job_id: str, evidence_id: str, profile_id: str = 'ruijie_aim_diag_v1', notify: bool = True):" in media_worker
    assert "if notify:" in pcm_worker
    assert "if notify:" in media_worker
    assert "if notify and \"job\" in locals() and job:" in pcm_worker
    assert "if notify and 'job' in locals() and job:" in media_worker


def test_live_golden_refresh_keeps_feishu_fail_closed_until_dtmf_ready():
    live = (ROOT / "tools/human_evidence_feishu_live_acceptance.py").read_text(encoding="utf-8")
    refresh = live.index("refresh = _refresh_stale_golden_dtmf")
    report = live.index("report, payload, _reused = generate_evidence_report")
    visual_gate = live.index("MISSING_REQUIRED_HUMAN_VISUALS")
    projection = live.index("projected_binding = await service.project")
    assert refresh < report < visual_gate < projection
    assert '"analyzer_refresh_performed"' in live
    assert '"analyzer_refresh_components"' in live
    assert '"dtmf_source_readiness_before"' in live
    assert '"dtmf_source_readiness_after"' in live
    assert '"feishu_projection_attempted": projected' in live
