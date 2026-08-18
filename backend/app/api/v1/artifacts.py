from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.evidence_permissions import EvidencePermission, has_evidence_permission, require_evidence_permission
from app.db.models import Artifact
from app.integrations.storage import ObjectStorage
from app.schemas.artifacts import ArtifactOut

router = APIRouter(tags=['artifacts'])

# Report-facing derived evidence is viewable with VIEW_REPORT. Full decoded audio,
# raw captures and bundles require explicit stronger capabilities.
REPORT_SAFE_TYPES = {
    'AUDIO_CLIP', 'WAVEFORM_PNG', 'SPECTRUM_PNG', 'SPECTROGRAM_PNG',
    'RTP_TIMELINE_PNG', 'SIP_CALL_FLOW_PNG', 'WAVEFORM_JSON', 'SPECTROGRAM_JSON',
    'PRELIMINARY_REPORT_HTML', 'PRELIMINARY_REPORT_JSON', 'MANIFEST_JSON',
}
BUNDLE_TYPES = {'EVIDENCE_BUNDLE'}
RAW_ARTIFACT_TYPES = {'PCM_WAV', 'RTP_WAV', 'AUDIO_WAV', 'RAW_PCAP'}


def _check_artifact_access(identity, artifact: Artifact, *, download: bool = False) -> None:
    atype = str(artifact.type or '').upper()
    if atype in BUNDLE_TYPES:
        if not has_evidence_permission(identity, EvidencePermission.DOWNLOAD_EVIDENCE_BUNDLE):
            raise HTTPException(403, 'EVIDENCE_BUNDLE_PERMISSION_REQUIRED')
        return
    if atype in REPORT_SAFE_TYPES:
        if not has_evidence_permission(identity, EvidencePermission.VIEW_REPORT):
            raise HTTPException(403, 'EVIDENCE_REPORT_PERMISSION_REQUIRED')
        return
    # Unknown analyzer artifacts and full WAV are treated as raw/deep evidence by
    # default. This is deliberately fail-closed.
    if not has_evidence_permission(identity, EvidencePermission.VIEW_RAW_EVIDENCE):
        raise HTTPException(403, 'RAW_EVIDENCE_PERMISSION_REQUIRED')


@router.get('/cases/{case_id}/artifacts', response_model=list[ArtifactOut])
def list_case_artifacts(
    case_id: str,
    db: Session = Depends(get_db),
    identity=Depends(require_evidence_permission(EvidencePermission.VIEW_REPORT)),
):
    rows = list(db.scalars(select(Artifact).where(Artifact.case_id == case_id).order_by(Artifact.created_at.asc())))
    if has_evidence_permission(identity, EvidencePermission.VIEW_RAW_EVIDENCE):
        return rows
    return [a for a in rows if str(a.type or '').upper() in REPORT_SAFE_TYPES]


@router.get('/artifacts/{artifact_id}', response_model=ArtifactOut)
def get_artifact(
    artifact_id: str,
    db: Session = Depends(get_db),
    identity=Depends(require_evidence_permission(EvidencePermission.VIEW_REPORT)),
):
    artifact = db.get(Artifact, artifact_id)
    if not artifact:
        raise HTTPException(404, 'ARTIFACT_NOT_FOUND')
    _check_artifact_access(identity, artifact)
    return artifact


@router.get('/analyzer-runs/{run_id}/artifacts', response_model=list[ArtifactOut])
def list_run_artifacts(
    run_id: str,
    db: Session = Depends(get_db),
    identity=Depends(require_evidence_permission(EvidencePermission.VIEW_REPORT)),
):
    rows = list(db.scalars(select(Artifact).where(Artifact.analyzer_run_id == run_id).order_by(Artifact.created_at.asc())))
    if has_evidence_permission(identity, EvidencePermission.VIEW_RAW_EVIDENCE):
        return rows
    return [a for a in rows if str(a.type or '').upper() in REPORT_SAFE_TYPES]


@router.get('/artifacts/{artifact_id}/download-url')
def artifact_download_url(
    artifact_id: str,
    db: Session = Depends(get_db),
    identity=Depends(require_evidence_permission(EvidencePermission.VIEW_REPORT)),
):
    artifact = db.get(Artifact, artifact_id)
    if not artifact:
        raise HTTPException(404, 'ARTIFACT_NOT_FOUND')
    _check_artifact_access(identity, artifact, download=True)
    return {'artifact_id': artifact.id, 'url': ObjectStorage().presigned_get(artifact.object_key)}


@router.get('/artifacts/{artifact_id}/content')
def artifact_content(
    artifact_id: str,
    db: Session = Depends(get_db),
    identity=Depends(require_evidence_permission(EvidencePermission.VIEW_REPORT)),
):
    artifact = db.get(Artifact, artifact_id)
    if not artifact:
        raise HTTPException(404, 'ARTIFACT_NOT_FOUND')
    _check_artifact_access(identity, artifact)
    return StreamingResponse(
        ObjectStorage().iter_object(artifact.object_key),
        media_type=artifact.content_type or 'application/octet-stream',
        headers={'Content-Disposition': f'inline; filename="{artifact.filename}"'},
    )
