from __future__ import annotations

import json
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.evidence_report_models import FeishuEvidenceDocumentBinding, PreliminaryEvidenceReport
from app.db.models import Artifact, Case
from app.integrations.feishu.transport import FeishuLiveTransport
from app.integrations.storage import ObjectStorage
from app.services.audit import audit
from app.services.evidence_report_artifacts import report_artifacts


class FeishuEvidenceDocumentService:
    """Projection of canonical report JSON into one Feishu Docx per Case.

    Feishu is presentation only. Any failure here must not alter the canonical
    PreliminaryEvidenceReport status or evidence authority.
    """
    def __init__(self, transport: FeishuLiveTransport|None=None, storage=None):
        self.transport=transport or FeishuLiveTransport(); self.storage=storage or ObjectStorage()

    @staticmethod
    def _text(content:str,block_type:int=2) -> dict:
        key={2:"text",3:"heading1",4:"heading2",5:"heading3",12:"bullet"}.get(block_type,"text")
        return {"block_type":block_type,key:{"elements":[{"text_run":{"content":content,"text_element_style":{}}}],"style":{}}}

    def _create_document(self,title:str) -> tuple[str,str|None]:
        data=self.transport._request("POST","/docx/v1/documents",json_body={"title":title})
        doc=(data.get("data") or {}).get("document") or {}
        document_id=doc.get("document_id") or doc.get("token")
        if not document_id: raise RuntimeError("FEISHU_DOCX_CREATE_MISSING_DOCUMENT_ID")
        return document_id,doc.get("url")

    def _insert_blocks(self,document_id:str,blocks:list[dict],index:int=0) -> list[dict]:
        created=[]; current=index
        for pos in range(0,len(blocks),40):
            chunk=blocks[pos:pos+40]
            response=self.transport._request("POST",f"/docx/v1/documents/{quote(document_id,safe='')}/blocks/{quote(document_id,safe='')}/children",
                                             json_body={"index":current,"children":chunk})
            rows=(response.get("data") or {}).get("children") or []
            created.extend(rows); current+=len(chunk)
        return created

    def _upload_media(self,*,block_id:str,filename:str,data:bytes,parent_type:str) -> str:
        token=self.transport._tenant_token()
        response=self.transport._client.post(self.transport._base+"/drive/v1/medias/upload_all",
            headers={"Authorization":f"Bearer {token}"},data={"file_name":filename,"parent_type":parent_type,"parent_node":block_id,"size":str(len(data))},
            files={"file":(filename,data,"application/octet-stream")})
        response.raise_for_status(); payload=response.json()
        if payload.get("code") not in (0,None): raise RuntimeError(f"FEISHU_MEDIA_UPLOAD_FAILED:{payload.get('code')}:{payload.get('msg')}")
        media_token=((payload.get("data") or {}).get("file_token"))
        if not media_token: raise RuntimeError("FEISHU_MEDIA_UPLOAD_MISSING_TOKEN")
        return media_token

    def _replace_media(self,document_id:str,block_id:str,token:str,*,image:bool) -> None:
        body={"replace_image":{"token":token}} if image else {"replace_file":{"token":token}}
        self.transport._request("PATCH",f"/docx/v1/documents/{quote(document_id,safe='')}/blocks/{quote(block_id,safe='')}",json_body=body)

    def _core_blocks(self,report:PreliminaryEvidenceReport,payload:dict) -> list[dict]:
        findings=payload.get("findings") or []; comp=payload.get("completeness") or {}; case=payload.get("case") or {}
        blocks=[
            self._text(f"V{report.version}｜{payload.get('generated_at')}｜{report.status}",3),
            self._text("0. 当前状态 / 快速导航",4),
            self._text(f"Case：{case.get('case_no')}｜范围：{report.scope_type}｜证据完整度：{comp.get('state')}｜问题点：{len(findings)}"),
            self._text("1. 当前初步结论",4), self._text(payload.get("headline") or ""),
            self._text((payload.get("evidence_boundary") or {}).get("statement") or ""),
            self._text("2. 当前重点问题",4),
        ]
        for f in findings[:20]:
            tr=f.get("time_range") or {}; blocks.extend([
                self._text(f"{f.get('severity')}｜{f.get('title')}",5),
                self._text(f"时间：{tr.get('start')} ～ {tr.get('end')}｜证据等级：{f.get('evidence_level')}"),
                self._text(f"已观测事实：{f.get('observation')}"), self._text(f"初步解释：{f.get('interpretation')}"),
                self._text(f"根因边界：{f.get('root_cause_boundary')}"),
            ])
        blocks.extend([self._text("3. 证据完整度",4)])
        for name,present in (comp.get("capture") or {}).items(): blocks.append(self._text(f"{'✅' if present else '⚠️'} {name}：{'可用' if present else '缺失/不可用'}",12))
        blocks.extend([self._text("4. 最新一次复现结果",4)])
        call=payload.get("call") or {}; blocks.append(self._text(f"Call：{call.get('call_no')}｜状态：{call.get('status')}｜开始：{call.get('started_at')}｜结束：{call.get('ended_at')}"))
        blocks.extend([self._text("5. 多次复现汇总",4),self._text("Case/Session 聚合结果以 Canonical Report JSON 为准；最新报告版本始终插入本文档顶部。")])
        blocks.extend([self._text("6. A/B 对比",4),self._text("存在可比较 Environment Fingerprint 时展示；不同环境不直接混合统计。")])
        blocks.extend([self._text("7. 历次 Reproduction Session（复现会话）",4),self._text("历史报告版本保留在本文档下方，最新优先。")])
        blocks.extend([self._text("8. 正常项 / 排除性证据",4)])
        for item in payload.get("normal_and_exclusion_evidence") or []: blocks.append(self._text(f"✅ {item.get('text')}",12))
        blocks.extend([self._text("9. 完整技术证据",4),self._text("RTP（实时传输协议）、PCM（脉冲编码调制）、dBFS（相对于数字满量程的分贝）等指标均可从附件与 Web 下钻复核。")])
        blocks.extend([self._text("10. Evidence Bundle / 附件",4),self._text("关键图片与异常音频附于本版本下方；完整原始证据通过 Evidence Bundle 获取。")])
        blocks.extend([self._text("11. 报告版本与审计记录",4),self._text(f"Schema：{payload.get('schema_version')}｜Composer：{payload.get('composer_version')}｜Report ID：{report.id}")])
        return blocks

    def project(self,db:Session,*,case_id:str,report_id:str) -> FeishuEvidenceDocumentBinding:
        case=db.get(Case,case_id); report=db.get(PreliminaryEvidenceReport,report_id)
        if not case or not report or report.case_id!=case_id: raise ValueError("FEISHU_EVIDENCE_REPORT_NOT_FOUND")
        payload=report.snapshot_json or {}
        binding=db.scalar(select(FeishuEvidenceDocumentBinding).where(FeishuEvidenceDocumentBinding.case_id==case_id).limit(1))
        if binding is None:
            document_id,url=self._create_document(f"{case.case_no} VOIP 初步证据分析报告")
            binding=FeishuEvidenceDocumentBinding(case_id=case_id,document_id=document_id,document_url=url,title=f"{case.case_no} VOIP 初步证据分析报告",status="CREATED")
            db.add(binding); db.flush()
        if not binding.document_id: raise RuntimeError("FEISHU_DOCUMENT_ID_MISSING")
        self._insert_blocks(binding.document_id,self._core_blocks(report,payload),index=0)
        # Images and abnormal clips are attached after the text projection. The
        # OpenAPI upload limit for this direct endpoint is 20 MiB; larger files
        # remain accessible from Evidence Bundle/Web rather than failing report projection.
        for artifact in report_artifacts(db,report.id):
            is_image=artifact.content_type=="image/png" and artifact.type in {"WAVEFORM_PNG","SPECTRUM_PNG","SPECTROGRAM_PNG","RTP_TIMELINE_PNG","SIP_CALL_FLOW_PNG"}
            is_clip=artifact.type in {"AUDIO_CLIP","PERIODIC_AUDIO_CLIP"}
            if not (is_image or is_clip) or artifact.size_bytes>20*1024*1024: continue
            placeholder={"block_type":27,"image":{}} if is_image else {"block_type":23,"file":{"token":""}}
            created=self._insert_blocks(binding.document_id,[placeholder],index=0)
            if not created: continue
            block_id=created[0].get("block_id")
            if not block_id: continue
            data=self.storage.get_bytes(artifact.object_key)
            media_token=self._upload_media(block_id=block_id,filename=artifact.filename,data=data,parent_type="docx_image" if is_image else "docx_file")
            self._replace_media(binding.document_id,block_id,media_token,image=is_image)
        binding.projected_report_id=report.id; binding.projection_version+=1; binding.status="SYNCED"; binding.last_error=None
        binding.metadata_json={"report_version":report.version,"report_status":report.status,"finding_count":payload.get("finding_count"),"ordering_contract":"D112"}
        audit(db,case_id=case_id,actor="feishu-evidence-document",event_type="FEISHU_EVIDENCE_DOCUMENT_SYNCED",target_type="feishu_evidence_document",target_id=binding.id,
              detail={"document_id":binding.document_id,"report_id":report.id,"report_version":report.version})
        db.flush(); return binding
