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
from app.reports.prd_spec_v1_alignment import finalize_report_contract
from app.services.audit import audit
from app.services.evidence_report_artifacts import report_artifacts


class FeishuEvidenceDocumentService:
    """One Case -> one Feishu Docx living projection of the canonical Evidence Report."""

    DOC_EDIT_INTERVAL_SECONDS = 0.38
    INLINE_MEDIA_LIMIT = 12
    LIVING_PROJECTION_CONTRACT = "feishu-evidence-living-document-v2"

    def __init__(self, transport: FeishuLiveTransport | None = None, storage=None):
        self.transport = transport or FeishuLiveTransport()
        self.storage = storage or ObjectStorage()

    @staticmethod
    def _text(content: str, block_type: int = 2) -> dict:
        key = {2: "text", 3: "heading1", 4: "heading2", 5: "heading3", 12: "bullet"}.get(block_type, "text")
        return {
            "block_type": block_type,
            key: {"elements": [{"text_run": {"content": str(content), "text_element_style": {}}}], "style": {}},
        }

    @staticmethod
    def _media_placeholder(*, image: bool) -> dict:
        return {"block_type": 27, "image": {}} if image else {"block_type": 23, "file": {"token": ""}}

    @staticmethod
    def _media_block_id(created_block: dict | None, *, image: bool) -> str | None:
        block = created_block or {}
        if image:
            return block.get("block_id") if block.get("block_type") in (None, 27) else None
        if block.get("block_type") == 23:
            return block.get("block_id")
        children = block.get("children") or []
        if block.get("block_type") == 33 and children:
            return children[0]
        return None

    async def _create_document(self, title: str) -> tuple[str, str | None]:
        data = await self.transport._request("POST", "/docx/v1/documents", json_body={"title": title})
        doc = (data.get("data") or {}).get("document") or {}
        document_id = doc.get("document_id") or doc.get("token")
        if not document_id:
            raise RuntimeError("FEISHU_DOCX_CREATE_MISSING_DOCUMENT_ID")
        return document_id, doc.get("url") or f"https://feishu.cn/docx/{document_id}"

    async def _insert_blocks(self, document_id: str, blocks: list[dict], index: int = 0) -> list[dict]:
        created = []
        current = index
        for pos in range(0, len(blocks), 40):
            chunk = blocks[pos:pos + 40]
            response = await self.transport._request(
                "POST",
                f"/docx/v1/documents/{quote(document_id, safe='')}/blocks/{quote(document_id, safe='')}/children",
                json_body={"index": current, "children": chunk},
            )
            rows = (response.get("data") or {}).get("children") or []
            created.extend(rows)
            current += len(chunk)
            time.sleep(self.DOC_EDIT_INTERVAL_SECONDS)
        return created

    async def _delete_tracked_projection(self, document_id: str, block_count: int) -> None:
        count = max(0, int(block_count or 0))
        if not count:
            return
        await self.transport._request(
            "DELETE",
            f"/docx/v1/documents/{quote(document_id, safe='')}/blocks/{quote(document_id, safe='')}/children/batch_delete",
            json_body={"start_index": 0, "end_index": count},
        )
        time.sleep(self.DOC_EDIT_INTERVAL_SECONDS)

    async def _upload_media(self, *, block_id: str, filename: str, data: bytes, parent_type: str) -> str:
        token = await self.transport._tenant_token()
        url = settings.feishu_base_url.rstrip("/") + "/drive/v1/medias/upload_all"
        async with httpx.AsyncClient(timeout=settings.feishu_timeout_seconds) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                data={"file_name": filename, "parent_type": parent_type, "parent_node": block_id, "size": str(len(data))},
                files={"file": (filename, data, "application/octet-stream")},
            )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") not in (0, None):
            raise RuntimeError(f"FEISHU_MEDIA_UPLOAD_FAILED:{payload.get('code')}:{payload.get('msg')}")
        media_token = (payload.get("data") or {}).get("file_token")
        if not media_token:
            raise RuntimeError("FEISHU_MEDIA_UPLOAD_MISSING_TOKEN")
        return media_token

    async def _replace_media(self, document_id: str, block_id: str, token: str, *, image: bool) -> None:
        body = {"replace_image": {"token": token}} if image else {"replace_file": {"token": token}}
        await self.transport._request(
            "PATCH",
            f"/docx/v1/documents/{quote(document_id, safe='')}/blocks/{quote(block_id, safe='')}",
            json_body=body,
        )
        time.sleep(self.DOC_EDIT_INTERVAL_SECONDS)

    @staticmethod
    def _scope_text(card: dict) -> str:
        scope = card.get("scope") or {}
        parts = []
        for label, key in (("层", "layer"), ("方向", "direction"), ("PCM Tap", "pcm_tap"), ("Call", "call_id"), ("RTP Stream", "rtp_stream_id"), ("SSRC", "ssrc")):
            if scope.get(key) not in (None, "", "UNKNOWN"):
                parts.append(f"{label}：{scope.get(key)}")
        return "｜".join(parts) if parts else "范围：UNKNOWN（当前 Evidence 不足以绑定更细 Scope）"

    @staticmethod
    def _time_text(card: dict) -> str:
        value = card.get("time") or {}
        start = value.get("absolute_start_utc")
        end = value.get("absolute_end_utc")
        if start is None and end is None:
            return "绝对时间：UNKNOWN（当前 Evidence 未提供可绑定时间）"
        absolute = f"{start or 'UNKNOWN'} ～ {end or start or 'UNKNOWN'}"
        relative = value.get("call_relative_representative") or value.get("call_relative_start")
        semantics = value.get("semantics") or "EVENT_OR_ANALYZER_RANGE"
        exact = value.get("exact_event_window_known")
        semantic_cn = {
            "OBSERVATION_BOUNDARY": "观察边界（非精确异常区间）",
            "ANALYSIS_WINDOW": "分析窗口（非精确异常区间）",
            "EVENT_OR_ANALYZER_RANGE": "Finding/Analyzer 时间范围",
            "UNKNOWN": "时间边界未知",
        }.get(str(semantics), str(semantics))
        return (
            f"绝对时间（UTC）：{absolute}｜时间语义：{semantic_cn}｜精确异常首末时刻：{'是' if exact else '否'}"
            + (f"｜Call 相对时间：{relative}" if relative else "")
        )

    @staticmethod
    def _action_name(value: str | None) -> str:
        return {
            "REQUEST_USER_EVIDENCE": "A/B 硬件变量验证",
            "REQUEST_MULTI_POINT_PCAP": "多点 PCAP 链路定位",
            "REVIEW_AND_VERIFY": "证据复核与验证",
        }.get(str(value or ""), str(value or "待执行验证"))

    @staticmethod
    def _risk_name(value: str | None) -> str:
        return {"USER": "人工执行", "LOW": "低风险", "MEDIUM": "中风险", "HIGH": "高风险"}.get(str(value or "").upper(), str(value or "未标注"))

    def _append_inline_media(self, blocks: list[dict], plan: list[dict], card: dict, *, budget: int) -> int:
        used = 0
        chosen = []
        for visual in (card.get("visual_evidence") or [])[:3]:
            chosen.append((visual, True))
        audio = card.get("audio_evidence") or {}
        if audio.get("status") == "AVAILABLE" and audio.get("clips"):
            chosen.append((audio["clips"][0], False))
        elif audio.get("status") == "UNAVAILABLE":
            blocks.append(self._text(f"⚠️ 异常音频：暂不可用｜{audio.get('reason')}", 12))
        for artifact, is_image in chosen:
            if used >= budget:
                break
            artifact_id = artifact.get("artifact_id")
            if not artifact_id:
                continue
            blocks.append(self._text(f"证据 Artifact：{artifact.get('type')}｜{artifact.get('caption')}", 12))
            block_index = len(blocks)
            blocks.append(self._media_placeholder(image=is_image))
            plan.append({"block_index": block_index, "artifact_id": artifact_id, "is_image": is_image, "finding_id": card.get("finding_id")})
            used += 1
        return used

    def _core_blocks(
        self,
        report: PreliminaryEvidenceReport,
        payload: dict,
        *,
        history_blocks: list[dict] | None = None,
    ) -> tuple[list[dict], int, list[dict]]:
        # Projection is rebuilt from one finalized Canonical Report; no prepend
        # revision/legacy override layer is allowed in V2.
        finalize_report_contract(report, payload)
        findings = payload.get("findings") or []
        comp = payload.get("completeness") or {}
        case = payload.get("case") or {}
        blocks: list[dict] = []
        inline_plan: list[dict] = []
        media_budget = self.INLINE_MEDIA_LIMIT
        context = payload.get("analysis_context") or {}
        offline = context.get("analysis_mode") == "OFFLINE_IMPORTED"
        frozen_comp = payload.get("capture_quality") or comp.get("frozen_v1") or {}
        review = comp.get("reviewability_contract") or {}

        blocks.extend([
            self._text(f"V{payload.get('version') or report.version}｜{payload.get('generated_at')}｜{payload.get('status') or report.status}", 3),
            self._text("0. 当前状态 / 快速导航", 4),
            self._text(
                f"Case：{case.get('case_no')}｜范围：{report.scope_type}｜证据完整度：{frozen_comp.get('state') or comp.get('state')}"
                f"｜可复核性：{review.get('state') or comp.get('reviewability') or 'UNKNOWN'}｜问题点：{len(findings)}｜最高等级：{payload.get('highest_severity')}"
            ),
        ])
        card_summary = payload.get("evidence_card_summary") or {}
        blocks.append(self._text(
            f"Evidence Card：{card_summary.get('finding_count', 0)}｜Scope已绑定：{card_summary.get('cards_with_bound_scope', 0)}"
            f"｜时间已绑定：{card_summary.get('cards_with_bound_time', 0)}｜有通过标准：{card_summary.get('cards_with_acceptance', 0)}"
            f"｜异常音频已匹配：{card_summary.get('audio_available_count', 0)}｜音频缺失：{card_summary.get('audio_unavailable_count', 0)}"
        ))

        blocks.extend([
            self._text("1. 当前初步结论", 4),
            self._text(payload.get("headline") or "当前没有可发布的初步结论。"),
            self._text((payload.get("evidence_boundary") or {}).get("statement") or "本报告仅描述当前 Evidence 边界，不确认最终根因。"),
        ])
        problem_scope = payload.get("problem_scope") or {}
        window = payload.get("observation_window") or {}
        next_actions = payload.get("next_actions") or []
        blocks.append(self._text("1.1 问题范围与绝对时间", 4))
        blocks.append(self._text(f"问题范围：{problem_scope.get('statement') or 'UNKNOWN（证据不足）'}"))
        blocks.append(self._text(f"已确认影响链路：{problem_scope.get('affected_path') or 'UNKNOWN（证据不足）'}"))
        for item in (problem_scope.get("excluded_or_weakened") or [])[:8]:
            blocks.append(self._text(f"已排除/明显弱化：{item}", 12))
        for item in (problem_scope.get("unresolved") or [])[:8]:
            blocks.append(self._text(f"尚未确认：{item}", 12))
        if window.get("absolute_start_utc") or window.get("absolute_end_utc"):
            blocks.append(self._text(f"观察窗口：{window.get('scope')}｜UTC {window.get('absolute_start_utc') or 'UNKNOWN'} ～ {window.get('absolute_end_utc') or 'UNKNOWN'}"))
            blocks.append(self._text(f"本地绝对时间（UTC+8）：{window.get('absolute_start_local') or 'UNKNOWN'} ～ {window.get('absolute_end_local') or 'UNKNOWN'}"))
        else:
            blocks.append(self._text("观察窗口：UNKNOWN（当前 Evidence 未提供绝对时间锚点）"))
        blocks.append(self._text(
            f"精确异常首末时刻已知：{'是' if window.get('exact_event_window_known') else '否'}｜{window.get('boundary_statement') or '不得将观察窗口伪装成精确异常区间。'}"
        ))

        blocks.append(self._text("1.2 下一步建议 / 验证顺序 / 通过标准", 4))
        if next_actions:
            for index, action in enumerate(next_actions, 1):
                blocks.append(self._text(
                    f"P{index - 1}｜{self._action_name(action.get('action_type'))}｜优先级 {action.get('priority')}｜{self._risk_name(action.get('risk_level'))}",
                    5,
                ))
                blocks.append(self._text(f"目的：{action.get('reason') or '补充可复核证据并收窄边界。'}"))
                for step in (action.get("execution_steps") or [])[:10]:
                    blocks.append(self._text(f"执行：{step}", 12))
                blocks.append(self._text(f"通过标准：{action.get('acceptance_criteria') or '必须产生可复核 Evidence 并更新诊断边界。'}"))
        else:
            blocks.append(self._text(f"下一步建议：{(payload.get('preliminary_assessment') or {}).get('recommended_next_action') or '当前没有可执行计划。'}"))

        blocks.append(self._text("2. 当前重点问题", 4))
        for finding in findings[:20]:
            card = finding.get("evidence_card") or {}
            first = ((finding.get("correlation") or {}).get("first_observable_boundary") or {})
            blocks.extend([
                self._text(f"{finding.get('severity')}｜{finding.get('title')}", 5),
                self._text(self._scope_text(card)),
                self._text(self._time_text(card)),
                self._text(f"发生了什么：{card.get('what_happened') or finding.get('observation') or 'UNKNOWN（证据不足）'}"),
                self._text(f"初步解释：{card.get('initial_interpretation') or finding.get('interpretation') or '仅记录当前 Evidence 事实。'}"),
            ])
            if first.get("statement"):
                blocks.append(self._text(first.get("statement")))
            elif first.get("status") == "UNKNOWN":
                blocks.append(self._text("首次可观测层：UNKNOWN（上游证据不足或不可比较）"))
            measurements = card.get("measurements") or []
            if measurements:
                blocks.append(self._text("关键测量："))
                for measurement in measurements[:8]:
                    unit = f" {measurement.get('unit')}" if measurement.get("unit") else ""
                    blocks.append(self._text(f"{measurement.get('label')}：{measurement.get('value')}{unit}", 12))
            packet_refs = card.get("packet_refs") or []
            if packet_refs:
                blocks.append(self._text("Packet / Frame 下钻："))
                for item in packet_refs[:8]:
                    blocks.append(self._text(
                        f"#{item.get('event')} Frame {item.get('previous_frame')}→{item.get('current_frame')}｜Seq {item.get('previous_seq')}→{item.get('current_seq')}"
                        f"｜Delta {item.get('delta_ms')} ms｜{item.get('classification')}",
                        12,
                    ))
            used = self._append_inline_media(blocks, inline_plan, card, budget=media_budget)
            media_budget = max(0, media_budget - used)
            blocks.append(self._text(f"当前不能确认什么：{card.get('root_cause_boundary') or finding.get('root_cause_boundary') or '具体物理根因尚未确认。'}"))
            blocks.append(self._text(f"下一步建议：{card.get('next_action') or finding.get('next_action') or '复核当前 Finding 的原始 Evidence。'}"))
            contract = card.get("action_contract") or finding.get("action_contract") or {}
            for step in (contract.get("execution_steps") or [])[:8]:
                blocks.append(self._text(f"验证步骤：{step}", 12))
            blocks.append(self._text(f"通过标准：{card.get('verification_acceptance') or finding.get('verification_acceptance') or '新增证据必须绑定本 Finding 并使证据边界发生可解释变化。'}"))

        blocks.append(self._text("3. 证据完整度", 4))
        dimensions = frozen_comp.get("dimensions") or {}
        if dimensions:
            for name in ("PCAP", "SIP", "RTP", "PCM_RX", "PCM_TX", "DEBUG", "CORRELATION"):
                item = dimensions.get(name) or {}
                available = bool(item.get("available"))
                requirement = "必需" if item.get("requirement") == "REQUIRED" else "可选"
                state_text = "可用" if available else "缺失/不可用"
                blocks.append(self._text(f"{'✅' if available else '⚠️'} {name}：{state_text}｜{requirement}｜{item.get('impact') or ''}", 12))
        else:
            blocks.append(self._text("证据完整度：UNKNOWN（Canonical completeness 未生成）"))
        blocks.append(self._text(f"完整度边界：{frozen_comp.get('boundary') or comp.get('boundary') or 'UNKNOWN'}"))

        call = payload.get("display_call") or payload.get("call") or {}
        if offline:
            blocks.append(self._text("4. 最新一次复现结果", 4))
            blocks.append(self._text(f"分析方式：离线证据导入｜复现 Session：不适用｜重建 Call：{context.get('reconstructed_call_count')}"))
            if call:
                blocks.append(self._text(
                    f"Call：{call.get('id') or call.get('call_no')}｜SIP Call-ID：{call.get('sip_call_id') or call.get('external_call_ref')}｜状态：{call.get('status')}"
                    f"｜开始：{call.get('started_at') or call.get('start_time')}｜结束：{call.get('ended_at') or call.get('end_time')}｜号码：{call.get('caller') or '?'} → {call.get('dialed_number') or '?'}"
                ))
            else:
                blocks.append(self._text("Call：UNKNOWN（离线 Evidence 无法唯一绑定诊断 Call）"))
        else:
            blocks.extend([
                self._text("4. 最新一次复现结果", 4),
                self._text(f"Call：{call.get('call_no') or call.get('id')}｜状态：{call.get('status')}｜开始：{call.get('started_at')}｜结束：{call.get('ended_at')}"),
            ])

        blocks.append(self._text("5. 多次复现汇总", 4))
        multi = payload.get("multi_call_summary") or {}
        if offline:
            blocks.append(self._text("当前离线重建 Call 仅用于证据展示，不计入 Reproduction Call 复现次数。"))
        if multi:
            blocks.append(self._text(f"有效 Reproduction Call 报告数：{multi.get('call_count')}"))
            for group in (multi.get("finding_groups") or [])[:20]:
                blocks.append(self._text(
                    f"{group.get('severity')}｜{group.get('title')}｜{group.get('occurrence_calls')}/{group.get('total_calls')}｜"
                    f"复现率 {round((group.get('reproduction_rate') or 0) * 100, 2)}%｜{group.get('stability')}",
                    12,
                ))
        else:
            blocks.append(self._text("当前没有可聚合的 Reproduction Call 报告。" if offline else "当前为 Call 级报告，无跨 Call 聚合。"))

        blocks.append(self._text("6. A/B 对比", 4))
        comparisons = payload.get("ab_comparison") or []
        if comparisons:
            for comp_item in comparisons[:6]:
                blocks.append(self._text(f"环境 A {str(comp_item.get('environment_a'))[:12]}… vs 环境 B {str(comp_item.get('environment_b'))[:12]}…", 5))
                for diff in (comp_item.get("differences") or [])[:20]:
                    if diff.get("significant_by_v1_rule"):
                        blocks.append(self._text(
                            f"{diff.get('title')}：A {round((diff.get('environment_a_rate') or 0) * 100, 2)}% → B {round((diff.get('environment_b_rate') or 0) * 100, 2)}%，"
                            f"差异 {round((diff.get('absolute_rate_delta') or 0) * 100, 2)}%；仅表示环境关联，不独立确认因果。",
                            12,
                        ))
        else:
            blocks.append(self._text("当前没有满足分组条件的 A/B 环境对比。"))
        baseline = payload.get("normal_baseline_comparison") or {}
        if baseline:
            if baseline.get("status") == "MATCHED":
                blocks.append(self._text(f"正常基线：已匹配同环境正常 Call {baseline.get('baseline_count')} 条；差异仅作为对照证据，不独立确认根因。", 12))
            else:
                blocks.append(self._text(f"正常基线：未使用｜{baseline.get('reason')}｜不强行匹配。", 12))

        blocks.append(self._text("7. 历次 Reproduction Session（复现会话）", 4))
        if offline:
            blocks.append(self._text("当前为离线证据导入；系统不会为了填充报告创建 ReproductionSession/ReproductionCall。"))
        else:
            blocks.append(self._text("Session/Call 按最新优先；单个 Call 内 Evidence 按时间正序。"))
        if history_blocks:
            blocks.extend(history_blocks)
        else:
            blocks.append(self._text("当前没有其他历史 Report 版本。", 12))

        blocks.append(self._text("8. 正常项 / 排除性证据", 4))
        for item in payload.get("normal_evidence") or payload.get("normal_and_exclusion_evidence") or []:
            blocks.append(self._text(f"✅ {item.get('text')}", 12))

        blocks.extend([
            self._text("9. 完整技术证据", 4),
            self._text("RTP（Real-time Transport Protocol，实时传输协议）、PCM（Pulse Code Modulation，脉冲编码调制）、dBFS（相对于数字满量程的分贝）等指标均应从 Finding → Evidence Card → Artifact/Frame/Analyzer 下钻复核。"),
        ])

        blocks.append(self._text("10. Evidence Bundle / 附件", 4))
        bundle = next((a for a in (payload.get("artifacts") or []) if str(a.get("type") or "").upper() == "EVIDENCE_BUNDLE"), None)
        manifest = next((a for a in (payload.get("artifacts") or []) if str(a.get("type") or "").upper() == "MANIFEST_JSON"), None)
        blocks.append(self._text(
            f"Evidence Bundle：{'已生成' if bundle else '当前投影未绑定'}｜Manifest：{'已生成' if manifest else '当前投影未绑定'}｜"
            f"Artifact Provenance：{'完整' if (payload.get('artifact_provenance_status') or {}).get('complete') else '需复核'}"
        ))
        blocks.append(self._text("关键证据优先放在对应 Evidence Card 下；此处追加未内联的图、音频和标准 Evidence Bundle。"))
        attachment_index = len(blocks)

        blocks.extend([
            self._text("11. 报告版本与审计记录", 4),
            self._text(
                f"Schema：{payload.get('schema') or payload.get('schema_version')}｜Composer：{payload.get('composer_version')}｜"
                f"Canonical Finalization：{(payload.get('canonical_finalization') or {}).get('contract_version')}｜Evidence Card：{card_summary.get('version')}｜Report ID：{report.id}"
            ),
            self._text("飞书仅为 Canonical Report 投影视图；人工修改飞书内容不改变后端 Evidence、Artifact、Finding 或 Root Cause Authority。"),
        ])
        return blocks, attachment_index, inline_plan

    def _history_blocks(self, db: Session, *, case_id: str, current_report_id: str) -> list[dict]:
        rows = list(db.scalars(select(PreliminaryEvidenceReport).where(
            PreliminaryEvidenceReport.case_id == case_id,
            PreliminaryEvidenceReport.id != current_report_id,
        ).order_by(PreliminaryEvidenceReport.created_at.desc()).limit(20)))
        blocks = []
        for row in rows:
            created = row.created_at.isoformat() if row.created_at else "UNKNOWN_TIME"
            blocks.append(self._text(f"历史 V{row.version}｜{row.scope_type}:{row.scope_id}｜{row.status}｜{created}｜Report {row.id}", 12))
        return blocks

    async def _materialize_plan(self, document_id: str, created_blocks: list[dict], plan: list[dict], artifact_by_id: dict) -> set[str]:
        used = set()
        for item in plan:
            raw_index = item.get("block_index")
            index = int(raw_index) if raw_index is not None else -1
            if not 0 <= index < len(created_blocks):
                continue
            artifact = artifact_by_id.get(item.get("artifact_id"))
            is_image = bool(item.get("is_image"))
            block_id = self._media_block_id(created_blocks[index], image=is_image)
            if not artifact or not block_id or artifact.size_bytes > 20 * 1024 * 1024:
                continue
            data = self.storage.get_bytes(artifact.object_key)
            token = await self._upload_media(
                block_id=block_id,
                filename=artifact.filename,
                data=data,
                parent_type="docx_image" if is_image else "docx_file",
            )
            await self._replace_media(document_id, block_id, token, image=is_image)
            used.add(artifact.id)
        return used

    async def project(self, db: Session, *, case_id: str, report_id: str) -> FeishuEvidenceDocumentBinding:
        case = db.get(Case, case_id)
        report = db.get(PreliminaryEvidenceReport, report_id)
        if not case or not report or report.case_id != case_id:
            raise ValueError("FEISHU_EVIDENCE_REPORT_NOT_FOUND")
        payload = report.snapshot_json or {}
        binding = db.scalar(select(FeishuEvidenceDocumentBinding).where(FeishuEvidenceDocumentBinding.case_id == case_id).limit(1))
        if binding is None:
            document_id, url = await self._create_document(f"{case.case_no} VOIP 初步证据分析报告")
            binding = FeishuEvidenceDocumentBinding(
                case_id=case_id,
                document_id=document_id,
                document_url=url,
                title=f"{case.case_no} VOIP 初步证据分析报告",
                status="CREATED",
            )
            db.add(binding)
            db.flush()
        if not binding.document_id:
            raise RuntimeError("FEISHU_DOCUMENT_ID_MISSING")

        previous_metadata = binding.metadata_json or {}
        previous_count = int(previous_metadata.get("projection_root_block_count") or 0)
        migration_mode = "TRACKED_REPLACE" if previous_count > 0 else ("LEGACY_UNTRACKED_PRESERVED" if binding.projection_version else "INITIAL")
        await self._delete_tracked_projection(binding.document_id, previous_count)

        artifacts = report_artifacts(db, report.id)
        artifact_by_id = {artifact.id: artifact for artifact in artifacts}
        history = self._history_blocks(db, case_id=case_id, current_report_id=report.id)
        core, attachment_index, inline_plan = self._core_blocks(report, payload, history_blocks=history)
        created_core = await self._insert_blocks(binding.document_id, core, index=0)
        inline_ids = await self._materialize_plan(binding.document_id, created_core, inline_plan, artifact_by_id)

        candidates = []
        for artifact in artifacts:
            if artifact.id in inline_ids:
                continue
            is_image = artifact.content_type == "image/png" and artifact.type in {
                "WAVEFORM_PNG", "SPECTRUM_PNG", "SPECTROGRAM_PNG", "RTP_TIMELINE_PNG", "SIP_CALL_FLOW_PNG"
            }
            is_clip = artifact.type in {"AUDIO_CLIP", "PERIODIC_AUDIO_CLIP"}
            is_bundle = artifact.type == "EVIDENCE_BUNDLE"
            if (is_image or is_clip or is_bundle) and artifact.size_bytes <= 20 * 1024 * 1024:
                candidates.append((artifact, is_image))
        candidates.sort(key=lambda pair: (
            0 if pair[1] else 1 if pair[0].type in {"AUDIO_CLIP", "PERIODIC_AUDIO_CLIP"} else 2,
            pair[0].created_at,
        ))
        candidates = candidates[:8]
        created_attachments = []
        if candidates:
            placeholders = [self._media_placeholder(image=is_image) for _, is_image in candidates]
            created_attachments = await self._insert_blocks(binding.document_id, placeholders, index=attachment_index)
            for (artifact, is_image), block in zip(candidates, created_attachments):
                block_id = self._media_block_id(block, image=is_image)
                if not block_id:
                    continue
                data = self.storage.get_bytes(artifact.object_key)
                token = await self._upload_media(
                    block_id=block_id,
                    filename=artifact.filename,
                    data=data,
                    parent_type="docx_image" if is_image else "docx_file",
                )
                await self._replace_media(binding.document_id, block_id, token, image=is_image)

        projection_root_block_count = len(created_core) + len(created_attachments)
        binding.projected_report_id = report.id
        binding.projection_version += 1
        binding.status = "SYNCED"
        binding.last_error = None
        binding.metadata_json = {
            "report_version": report.version,
            "report_status": report.status,
            "finding_count": payload.get("finding_count"),
            "ordering_contract": "D112",
            "living_document_contract": self.LIVING_PROJECTION_CONTRACT,
            "projection_mode": "REPLACE_TRACKED_ROOT_RANGE_V2",
            "projection_root_block_count": projection_root_block_count,
            "migration_mode": migration_mode,
            "inline_evidence_count": len(inline_ids),
            "attachment_count": len(candidates),
            "analysis_mode": (payload.get("analysis_context") or {}).get("analysis_mode"),
            "call_origin": (payload.get("analysis_context") or {}).get("call_origin"),
            "single_canonical_fact_layer": True,
        }
        audit(
            db,
            case_id=case_id,
            actor="feishu-evidence-document",
            event_type="FEISHU_EVIDENCE_DOCUMENT_SYNCED",
            target_type="feishu_evidence_document",
            target_id=binding.id,
            detail={
                "document_id": binding.document_id,
                "report_id": report.id,
                "report_version": report.version,
                "inline_evidence_count": len(inline_ids),
                "attachment_count": len(candidates),
                "living_document_contract": self.LIVING_PROJECTION_CONTRACT,
                "projection_root_block_count": projection_root_block_count,
                "migration_mode": migration_mode,
                "analysis_mode": (payload.get("analysis_context") or {}).get("analysis_mode"),
            },
        )
        db.flush()
        return binding
