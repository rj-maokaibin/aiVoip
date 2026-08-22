from __future__ import annotations

import time
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.evidence_report_models import FeishuEvidenceDocumentBinding, PreliminaryEvidenceReport
from app.db.models import Case
from app.integrations.feishu.transport import FeishuLiveTransport
from app.integrations.storage import ObjectStorage
from app.services.audit import audit
from app.services.evidence_report_artifacts import report_artifacts


class FeishuEvidenceDocumentService:
    """One Case -> one Feishu Docx projection of the canonical Evidence Report."""
    DOC_EDIT_INTERVAL_SECONDS=0.38
    INLINE_MEDIA_LIMIT=12

    def __init__(self,transport:FeishuLiveTransport|None=None,storage=None):
        self.transport=transport or FeishuLiveTransport();self.storage=storage or ObjectStorage()

    @staticmethod
    def _text(content:str,block_type:int=2)->dict:
        key={2:"text",3:"heading1",4:"heading2",5:"heading3",12:"bullet"}.get(block_type,"text")
        return {"block_type":block_type,key:{"elements":[{"text_run":{"content":str(content),"text_element_style":{}}}],"style":{}}}

    @staticmethod
    def _media_placeholder(*,image:bool)->dict:
        return {"block_type":27,"image":{}} if image else {"block_type":23,"file":{"token":""}}

    async def _create_document(self,title:str)->tuple[str,str|None]:
        data=await self.transport._request("POST","/docx/v1/documents",json_body={"title":title})
        doc=(data.get("data") or {}).get("document") or {};document_id=doc.get("document_id") or doc.get("token")
        if not document_id:raise RuntimeError("FEISHU_DOCX_CREATE_MISSING_DOCUMENT_ID")
        return document_id,doc.get("url") or f"https://feishu.cn/docx/{document_id}"

    async def _insert_blocks(self,document_id:str,blocks:list[dict],index:int=0)->list[dict]:
        created=[];current=index
        for pos in range(0,len(blocks),40):
            chunk=blocks[pos:pos+40]
            response=await self.transport._request("POST",f"/docx/v1/documents/{quote(document_id,safe='')}/blocks/{quote(document_id,safe='')}/children",
                                                   json_body={"index":current,"children":chunk})
            rows=(response.get("data") or {}).get("children") or [];created.extend(rows);current+=len(chunk);time.sleep(self.DOC_EDIT_INTERVAL_SECONDS)
        return created

    async def _upload_media(self,*,block_id:str,filename:str,data:bytes,parent_type:str)->str:
        token=await self.transport._tenant_token();url=settings.feishu_base_url.rstrip("/")+"/drive/v1/medias/upload_all"
        async with httpx.AsyncClient(timeout=settings.feishu_timeout_seconds) as client:
            response=await client.post(url,headers={"Authorization":f"Bearer {token}"},
                data={"file_name":filename,"parent_type":parent_type,"parent_node":block_id,"size":str(len(data))},
                files={"file":(filename,data,"application/octet-stream")})
        response.raise_for_status();payload=response.json()
        if payload.get("code") not in (0,None):raise RuntimeError(f"FEISHU_MEDIA_UPLOAD_FAILED:{payload.get('code')}:{payload.get('msg')}")
        media_token=(payload.get("data") or {}).get("file_token")
        if not media_token:raise RuntimeError("FEISHU_MEDIA_UPLOAD_MISSING_TOKEN")
        return media_token

    async def _replace_media(self,document_id:str,block_id:str,token:str,*,image:bool)->None:
        body={"replace_image":{"token":token}} if image else {"replace_file":{"token":token}}
        await self.transport._request("PATCH",f"/docx/v1/documents/{quote(document_id,safe='')}/blocks/{quote(block_id,safe='')}",json_body=body)
        time.sleep(self.DOC_EDIT_INTERVAL_SECONDS)

    @staticmethod
    def _scope_text(card:dict)->str:
        scope=card.get("scope") or {};parts=[]
        for label,key in (("层","layer"),("方向","direction"),("Tap","pcm_tap"),("Call","call_id"),("SSRC","ssrc")):
            if scope.get(key) not in (None,""):parts.append(f"{label}：{scope.get(key)}")
        return "｜".join(parts) or "范围：未绑定"

    @staticmethod
    def _time_text(card:dict)->str:
        t=card.get("time") or {};absolute=f"{t.get('absolute_start_utc')} ～ {t.get('absolute_end_utc')}"
        relative=t.get("call_relative_representative") or t.get("call_relative_start")
        return f"绝对时间（UTC）：{absolute}"+(f"｜Call 相对时间：{relative}" if relative else "")

    def _append_inline_media(self,blocks:list[dict],plan:list[dict],card:dict,*,budget:int)->int:
        used=0
        chosen=[]
        for visual in (card.get("visual_evidence") or [])[:2]:chosen.append((visual,True))
        audio=card.get("audio_evidence") or {}
        if audio.get("status")=="AVAILABLE" and audio.get("clips"):chosen.append((audio["clips"][0],False))
        elif audio.get("status")=="UNAVAILABLE":blocks.append(self._text(f"⚠️ 异常音频：UNAVAILABLE｜{audio.get('reason')}",12))
        for artifact,is_image in chosen:
            if used>=budget:break
            artifact_id=artifact.get("artifact_id")
            if not artifact_id:continue
            blocks.append(self._text(f"证据 Artifact：{artifact.get('type')}｜{artifact.get('caption')}",12))
            block_index=len(blocks);blocks.append(self._media_placeholder(image=is_image))
            plan.append({"block_index":block_index,"artifact_id":artifact_id,"is_image":is_image,"finding_id":card.get("finding_id")})
            used+=1
        return used

    def _core_blocks(self,report:PreliminaryEvidenceReport,payload:dict)->tuple[list[dict],int,list[dict]]:
        findings=payload.get("findings") or [];comp=payload.get("completeness") or {};case=payload.get("case") or {};blocks=[];inline_plan=[];media_budget=self.INLINE_MEDIA_LIMIT
        context=payload.get("analysis_context") or {};offline=context.get("analysis_mode")=="OFFLINE_IMPORTED"
        blocks.extend([self._text(f"V{report.version}｜{payload.get('generated_at')}｜{report.status}",3),self._text("0. 当前状态 / 快速导航",4),
                       self._text(f"Case：{case.get('case_no')}｜范围：{report.scope_type}｜证据完整度：{comp.get('state')}｜可复核性：{comp.get('reviewability')}｜问题点：{len(findings)}｜最高等级：{payload.get('highest_severity')}")])
        card_summary=payload.get("evidence_card_summary") or {}
        if card_summary:blocks.append(self._text(f"Evidence Card：{card_summary.get('finding_count')}｜需音频：{card_summary.get('audio_expected_count')}｜已匹配：{card_summary.get('audio_available_count')}｜缺失：{card_summary.get('audio_unavailable_count')}"))
        blocks.extend([self._text("1. 当前初步结论",4),self._text(payload.get("headline") or ""),self._text((payload.get("evidence_boundary") or {}).get("statement") or "")])
        blocks.append(self._text("2. 当前重点问题",4))
        for f in findings[:20]:
            card=f.get("evidence_card") or {};first=((f.get("correlation") or {}).get("first_observable_boundary") or {})
            blocks.extend([self._text(f"{f.get('severity')}｜{f.get('title')}",5),self._text(self._scope_text(card)),self._text(self._time_text(card)),
                           self._text(f"发生了什么：{card.get('what_happened') or f.get('observation')}"),self._text(f"初步解释：{card.get('initial_interpretation') or f.get('interpretation')}"),
                           self._text(first.get("statement") or ("首次可观测层：UNKNOWN（未知）" if first.get("status")=="UNKNOWN" else ""))])
            measurements=card.get("measurements") or []
            if measurements:
                blocks.append(self._text("关键测量："))
                for m in measurements[:8]:blocks.append(self._text(f"{m.get('label')}：{m.get('value')}{' '+str(m.get('unit')) if m.get('unit') else ''}",12))
            packet_refs=card.get("packet_refs") or []
            if packet_refs:
                blocks.append(self._text("Packet / Frame 下钻："))
                for x in packet_refs[:8]:blocks.append(self._text(f"#{x.get('event')} Frame {x.get('previous_frame')}→{x.get('current_frame')}｜Seq {x.get('previous_seq')}→{x.get('current_seq')}｜Delta {x.get('delta_ms')} ms｜{x.get('classification')}",12))
            used=self._append_inline_media(blocks,inline_plan,card,budget=media_budget);media_budget=max(0,media_budget-used)
            blocks.extend([self._text(f"当前不能确认什么：{card.get('root_cause_boundary') or f.get('root_cause_boundary')}"),
                           self._text(f"下一步建议：{card.get('next_action')}")])
        blocks.append(self._text("3. 证据完整度",4))
        for name,present in (comp.get("capture") or {}).items():blocks.append(self._text(f"{'✅' if present else '⚠️'} {name}：{'可用' if present else '缺失/不可用'}",12))
        call=payload.get("display_call") or payload.get("call") or {}
        if offline:
            blocks.append(self._text("4. 当前离线 Call 重建结果",4));blocks.append(self._text(f"分析方式：离线证据导入｜复现 Session：不适用｜重建 Call：{context.get('reconstructed_call_count')}"))
            if call:blocks.append(self._text(f"Call：{call.get('id') or call.get('call_no')}｜SIP Call-ID：{call.get('sip_call_id') or call.get('external_call_ref')}｜状态：{call.get('status')}｜开始：{call.get('started_at')}｜结束：{call.get('ended_at')}｜号码：{call.get('caller') or '?'} → {call.get('dialed_number') or '?'}"))
            else:blocks.append(self._text(f"Call：未绑定｜Call Scope：{context.get('call_scope')}｜Call Origin：{context.get('call_origin')}"))
        else:
            blocks.extend([self._text("4. 最新一次复现结果",4),self._text(f"Call：{call.get('call_no')}｜状态：{call.get('status')}｜开始：{call.get('started_at')}｜结束：{call.get('ended_at')}")])
        blocks.append(self._text("5. 多次复现汇总",4));multi=payload.get("multi_call_summary") or {}
        if offline:blocks.append(self._text("当前离线重建 Call 仅用于证据展示，不计入 Reproduction Call 复现次数。"))
        if multi:
            blocks.append(self._text(f"有效 Reproduction Call 报告数：{multi.get('call_count')}"))
            for g in (multi.get("finding_groups") or [])[:20]:blocks.append(self._text(f"{g.get('severity')}｜{g.get('title')}｜{g.get('occurrence_calls')}/{g.get('total_calls')}｜复现率 {round((g.get('reproduction_rate') or 0)*100,2)}%｜{g.get('stability')}",12))
        else:blocks.append(self._text("当前没有可聚合的 Reproduction Call 报告。" if offline else "当前为 Call 级报告，无跨 Call 聚合。"))
        blocks.append(self._text("6. A/B 对比",4));comparisons=payload.get("ab_comparison") or []
        if comparisons:
            for comp_item in comparisons[:6]:
                blocks.append(self._text(f"环境 A {str(comp_item.get('environment_a'))[:12]}… vs 环境 B {str(comp_item.get('environment_b'))[:12]}…",5))
                for diff in (comp_item.get("differences") or [])[:20]:
                    if diff.get("significant_by_v1_rule"):blocks.append(self._text(f"{diff.get('title')}：A {round((diff.get('environment_a_rate') or 0)*100,2)}% → B {round((diff.get('environment_b_rate') or 0)*100,2)}%，差异 {round((diff.get('absolute_rate_delta') or 0)*100,2)}%；仅表示环境关联，不独立确认因果。",12))
        else:blocks.append(self._text("当前没有满足分组条件的 A/B 环境对比。"))
        blocks.append(self._text("7. 历次 Reproduction Session（复现会话）",4));blocks.append(self._text("当前为离线证据导入；系统不会为了填充报告创建 ReproductionSession/ReproductionCall。历史真实 Reproduction 报告仍按 Case 维度保留。" if offline else "历史报告版本保留在本文档下方；版本之间按最新优先，同一 Call 内事件按时间正序。"))
        blocks.append(self._text("8. 正常项 / 排除性证据",4))
        for item in payload.get("normal_and_exclusion_evidence") or []:blocks.append(self._text(f"✅ {item.get('text')}",12))
        blocks.extend([self._text("9. 完整技术证据",4),self._text("RTP（Real-time Transport Protocol，实时传输协议）、PCM（Pulse Code Modulation，脉冲编码调制）、dBFS（Decibels relative to Full Scale，相对于数字满量程的分贝）等指标可从 Web/Artifact 下钻复核。")])
        blocks.extend([self._text("10. Evidence Bundle / 附件",4),self._text("关键证据已优先放到对应 Evidence Card 下；此处仅追加未内联的图、音频和 Evidence Bundle。")])
        attachment_index=len(blocks)
        blocks.extend([self._text("11. 报告版本与审计记录",4),self._text(f"Schema：{payload.get('schema_version')}｜Composer：{payload.get('composer_version')}｜Evidence Card：{card_summary.get('version')}｜Report ID：{report.id}")])
        return blocks,attachment_index,inline_plan

    async def _materialize_plan(self,document_id:str,created_blocks:list[dict],plan:list[dict],artifact_by_id:dict)->set[str]:
        used=set()
        for item in plan:
            raw_index=item.get("block_index");index=int(raw_index) if raw_index is not None else -1
            if not 0<=index<len(created_blocks):continue
            artifact=artifact_by_id.get(item.get("artifact_id"));block_id=(created_blocks[index] or {}).get("block_id")
            if not artifact or not block_id or artifact.size_bytes>20*1024*1024:continue
            data=self.storage.get_bytes(artifact.object_key);is_image=bool(item.get("is_image"))
            await self._upload_media(block_id=block_id,filename=artifact.filename,data=data,parent_type="docx_image" if is_image else "docx_file")
            used.add(artifact.id)
        return used

    async def project(self,db:Session,*,case_id:str,report_id:str)->FeishuEvidenceDocumentBinding:
        case=db.get(Case,case_id);report=db.get(PreliminaryEvidenceReport,report_id)
        if not case or not report or report.case_id!=case_id:raise ValueError("FEISHU_EVIDENCE_REPORT_NOT_FOUND")
        payload=report.snapshot_json or {};binding=db.scalar(select(FeishuEvidenceDocumentBinding).where(FeishuEvidenceDocumentBinding.case_id==case_id).limit(1))
        if binding is None:
            document_id,url=await self._create_document(f"{case.case_no} VOIP 初步证据分析报告")
            binding=FeishuEvidenceDocumentBinding(case_id=case_id,document_id=document_id,document_url=url,title=f"{case.case_no} VOIP 初步证据分析报告",status="CREATED");db.add(binding);db.flush()
        if not binding.document_id:raise RuntimeError("FEISHU_DOCUMENT_ID_MISSING")
        artifacts=report_artifacts(db,report.id);artifact_by_id={a.id:a for a in artifacts}
        core,attachment_index,inline_plan=self._core_blocks(report,payload);created_core=await self._insert_blocks(binding.document_id,core,index=0)
        inline_ids=await self._materialize_plan(binding.document_id,created_core,inline_plan,artifact_by_id)

        candidates=[]
        for artifact in artifacts:
            if artifact.id in inline_ids:continue
            is_image=artifact.content_type=="image/png" and artifact.type in {"WAVEFORM_PNG","SPECTRUM_PNG","SPECTROGRAM_PNG","RTP_TIMELINE_PNG","SIP_CALL_FLOW_PNG"}
            is_clip=artifact.type in {"AUDIO_CLIP","PERIODIC_AUDIO_CLIP"};is_bundle=artifact.type=="EVIDENCE_BUNDLE"
            if (is_image or is_clip or is_bundle) and artifact.size_bytes<=20*1024*1024:candidates.append((artifact,is_image))
        candidates.sort(key=lambda pair:(0 if pair[1] else 1 if pair[0].type in {"AUDIO_CLIP","PERIODIC_AUDIO_CLIP"} else 2,pair[0].created_at));candidates=candidates[:8]
        if candidates:
            placeholders=[self._media_placeholder(image=is_image) for _,is_image in candidates];created=await self._insert_blocks(binding.document_id,placeholders,index=attachment_index)
            for (artifact,is_image),block in zip(candidates,created):
                block_id=block.get("block_id")
                if not block_id:continue
                data=self.storage.get_bytes(artifact.object_key)
                await self._upload_media(block_id=block_id,filename=artifact.filename,data=data,parent_type="docx_image" if is_image else "docx_file")
        binding.projected_report_id=report.id;binding.projection_version+=1;binding.status="SYNCED";binding.last_error=None
        binding.metadata_json={"report_version":report.version,"report_status":report.status,"finding_count":payload.get("finding_count"),"ordering_contract":"D112",
                               "inline_evidence_count":len(inline_ids),"attachment_count":len(candidates),"analysis_mode":(payload.get("analysis_context") or {}).get("analysis_mode"),
                               "call_origin":(payload.get("analysis_context") or {}).get("call_origin")}
        audit(db,case_id=case_id,actor="feishu-evidence-document",event_type="FEISHU_EVIDENCE_DOCUMENT_SYNCED",target_type="feishu_evidence_document",target_id=binding.id,
              detail={"document_id":binding.document_id,"report_id":report.id,"report_version":report.version,"inline_evidence_count":len(inline_ids),"attachment_count":len(candidates),
                      "analysis_mode":(payload.get("analysis_context") or {}).get("analysis_mode")})
        db.flush();return binding