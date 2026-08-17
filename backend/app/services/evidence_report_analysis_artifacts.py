from __future__ import annotations

from sqlalchemy.orm import Session

from app.contracts.evidence_report import EvidenceReportArtifactType
from app.db.evidence_report_models import PreliminaryEvidenceReport
from app.db.models import AnalyzerRun, Artifact
from app.services.evidence_report_artifacts import persist_artifact


_TYPE_BY_ANALYZER={
    "packet_intelligence":EvidenceReportArtifactType.PACKET_ANALYSIS_JSON.value,
    "pcm_intelligence":EvidenceReportArtifactType.PCM_ANALYSIS_JSON.value,
    "media_intelligence":EvidenceReportArtifactType.MEDIA_ANALYSIS_JSON.value,
}
_FILE_BY_ANALYZER={
    "packet_intelligence":"packet_analysis.json",
    "pcm_intelligence":"pcm_analysis.json",
    "media_intelligence":"media_analysis.json",
}


def materialize_analyzer_json_artifacts(db:Session,storage,*,report:PreliminaryEvidenceReport,runs:dict[str,AnalyzerRun])->list[Artifact]:
    out=[]
    for analyzer_name in ("packet_intelligence","pcm_intelligence","media_intelligence"):
        run=runs.get(analyzer_name)
        if not run or run.status not in {"SUCCESS","PARTIAL_SUCCESS"} or not run.result_object_key:
            continue
        try:data=storage.get_bytes(run.result_object_key)
        except Exception:continue
        out.append(persist_artifact(db,storage,report=report,artifact_type=_TYPE_BY_ANALYZER[analyzer_name],filename=_FILE_BY_ANALYZER[analyzer_name],
            data=data,content_type="application/json",metadata={"source_object_key":run.result_object_key,"analyzer_name":analyzer_name,
                "analyzer_version":run.analyzer_version,"config_version":run.config_version,"config_checksum":run.config_checksum},
            analyzer_run_id=run.id,role="ANALYZER_RESULT"))
    return out
