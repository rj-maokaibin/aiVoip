from app.contracts.evidence_report import EvidenceReportArtifactType


def test_evidence_bundle_artifact_contract_contains_required_types():
    required={
        "PRELIMINARY_REPORT_HTML","PRELIMINARY_REPORT_JSON","MANIFEST_JSON","EVIDENCE_BUNDLE",
        "PACKET_ANALYSIS_JSON","PCM_ANALYSIS_JSON","MEDIA_ANALYSIS_JSON","SPECTRUM_PNG","SPECTROGRAM_PNG","WAVEFORM_PNG",
    }
    values={x.value for x in EvidenceReportArtifactType}
    assert required.issubset(values)
