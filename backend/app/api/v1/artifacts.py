from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.db.models import Artifact
from app.integrations.storage import ObjectStorage
from app.schemas.artifacts import ArtifactOut

router = APIRouter(tags=['artifacts'])


@router.get('/cases/{case_id}/artifacts', response_model=list[ArtifactOut])
def list_case_artifacts(case_id: str, db: Session = Depends(get_db)):
    return list(db.scalars(select(Artifact).where(Artifact.case_id == case_id).order_by(Artifact.created_at.asc())))

@router.get('/artifacts/{artifact_id}', response_model=ArtifactOut)
def get_artifact(artifact_id: str, db: Session = Depends(get_db)):
    artifact = db.get(Artifact, artifact_id)
    if not artifact:
        raise HTTPException(404, 'ARTIFACT_NOT_FOUND')
    return artifact

@router.get('/analyzer-runs/{run_id}/artifacts', response_model=list[ArtifactOut])
def list_run_artifacts(run_id: str, db: Session = Depends(get_db)):
    return list(db.scalars(select(Artifact).where(Artifact.analyzer_run_id == run_id).order_by(Artifact.created_at.asc())))

@router.get('/artifacts/{artifact_id}/download-url')
def artifact_download_url(artifact_id: str, db: Session = Depends(get_db)):
    artifact = db.get(Artifact, artifact_id)
    if not artifact:
        raise HTTPException(404, 'ARTIFACT_NOT_FOUND')
    return {'artifact_id': artifact.id, 'url': ObjectStorage().presigned_get(artifact.object_key)}


@router.get('/artifacts/{artifact_id}/content')
def artifact_content(artifact_id: str, db: Session = Depends(get_db)):
    artifact = db.get(Artifact, artifact_id)
    if not artifact:
        raise HTTPException(404, 'ARTIFACT_NOT_FOUND')
    return StreamingResponse(ObjectStorage().iter_object(artifact.object_key), media_type=artifact.content_type or 'application/octet-stream', headers={'Content-Disposition': f'inline; filename=\"{artifact.filename}\"'})
