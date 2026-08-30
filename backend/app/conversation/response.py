from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.conversation.contracts import ResponsePlan
from app.conversation.entities import resolve_catalog_entities
from app.conversation.gateway import ConversationGatewayClient, ConversationGatewayError
from app.conversation.snapshot import ConversationSnapshotBuilder
from app.conversation.state_service import ConversationStateService, slot_label
from app.core.config import settings


class GroundedConversationResponder:
    """Render natural Chinese replies from deterministic truth catalogs.

    The optional LLM only selects catalog IDs. All user-visible diagnosis facts are
    resolved from the Case snapshot. Knowledge text comes only from the grounded
    Knowledge service (ProductFact or reviewer-verified KnowledgeItem), never from
    free model synthesis.
    """

    def __init__(self, gateway: ConversationGatewayClient | None = None):
        self.gateway = gateway or ConversationGatewayClient()
        self.snapshot_builder = ConversationSnapshotBuilder()

    def render(
        self,
        db: Session,
        *,
        case_id: str,
        intent: str,
        interpretation: dict[str, Any] | None = None,
    ) -> str:
        interpretation = interpretation or {}
        snapshot = self.snapshot_builder.build(db, case_id)

        # Knowledge inside a Case is allowed without changing diagnosis state.
        if intent in {"KNOWLEDGE_IN_CASE", "HYBRID_KNOWLEDGE_DIAGNOSIS"}:
            knowledge = self._knowledge_answer(db, case_id, interpretation)
            if intent == "KNOWLEDGE_IN_CASE":
                return knowledge
            diagnosis_reply = self._deterministic_render(snapshot, intent, interpretation)
            if knowledge:
                return f"{knowledge}\n\n{diagnosis_reply}"
            return diagnosis_reply

        plan = self._plan(snapshot=snapshot, intent=intent)
        if plan is not None:
            rendered = self._render_plan(snapshot, plan)
            if rendered:
                return rendered
        return self._deterministic_render(snapshot, intent, interpretation)

    @staticmethod
    def _knowledge_answer(db: Session, case_id: str, interpretation: dict[str, Any]) -> str:
        from app.knowledge.conversation_service import answer_grounded_knowledge

        parsed_entities = dict(interpretation.get("entities") or {})
        query = str(parsed_entities.get("knowledge_query") or parsed_entities.get("incident_context") or "").strip()
        if not query:
            return "当前没有解析出明确的知识查询内容；我不会凭空补一个规格或协议结论。"

        conversation, _state = ConversationStateService().case_state(db, case_id)
        persisted = dict((conversation.entities_json or {}) if conversation else {})
        grounded_entities = resolve_catalog_entities(db, query, persisted)
        grounded_entities.update({
            key: value for key, value in parsed_entities.items()
            if value not in (None, "", [], {}) and key not in {"knowledge_query", "incident_context"}
        })
        if conversation is not None and grounded_entities != persisted:
            conversation.entities_json = grounded_entities
            db.flush()
        answer = answer_grounded_knowledge(db, query, entities=grounded_entities)
        return str(answer.get("text") or "").strip()

    def _plan(self, *, snapshot: dict[str, Any], intent: str) -> ResponsePlan | None:
        if not settings.grounded_response_enabled or not self.gateway.enabled():
            return None
        try:
            raw = self.gateway.plan_response(snapshot=snapshot, intent=intent)
            plan = ResponsePlan.model_validate(raw["proposal"])
            facts = snapshot.get("fact_catalog") or {}
            uncertainties = snapshot.get("uncertainty_catalog") or {}
            actions = snapshot.get("allowed_actions") or {}
            questions = snapshot.get("question_catalog") or {}
            if any(key not in facts for key in plan.fact_ids):
                raise ValueError("RESPONSE_PLAN_UNKNOWN_FACT_ID")
            if any(key not in uncertainties for key in plan.uncertainty_ids):
                raise ValueError("RESPONSE_PLAN_UNKNOWN_UNCERTAINTY_ID")
            if plan.next_action_id and plan.next_action_id not in actions:
                raise ValueError("RESPONSE_PLAN_UNKNOWN_ACTION_ID")
            if plan.question_id and plan.question_id not in questions:
                raise ValueError("RESPONSE_PLAN_UNKNOWN_QUESTION_ID")
            return plan
        except (ConversationGatewayError, ValidationError, ValueError, KeyError, TypeError):
            return None

    @staticmethod
    def _clean_item(value: Any) -> str:
        return str(value or "").strip().rstrip("。；;，, ")

    @classmethod
    def _bullet_section(cls, title: str, items: list[Any]) -> str:
        cleaned = [cls._clean_item(item) for item in items]
        cleaned = [item for item in cleaned if item]
        if not cleaned:
            return ""
        return title + "\n" + "\n".join(f"• {item}" for item in cleaned)

    @staticmethod
    def _render_plan(snapshot: dict[str, Any], plan: ResponsePlan) -> str:
        facts = snapshot.get("fact_catalog") or {}
        uncertainties = snapshot.get("uncertainty_catalog") or {}
        actions = snapshot.get("allowed_actions") or {}
        questions = snapshot.get("question_catalog") or {}
        parts = [str(facts[key]).rstrip("。") for key in plan.fact_ids if facts.get(key)]
        parts += [str(uncertainties[key]).rstrip("。") for key in plan.uncertainty_ids if uncertainties.get(key)]
        if plan.next_action_id and actions.get(plan.next_action_id):
            parts.append(str(actions[plan.next_action_id]).rstrip("。"))
        if plan.question_id and questions.get(plan.question_id):
            parts.append(str(questions[plan.question_id]).rstrip("。"))
        return "。".join(part for part in parts if part) + ("。" if parts else "")

    def _deterministic_render(
        self,
        snapshot: dict[str, Any],
        intent: str,
        interpretation: dict[str, Any],
    ) -> str:
        case = snapshot["case"]
        runtime = snapshot["runtime"]
        diagnosis = snapshot["diagnosis"]
        conversation = snapshot["conversation"]
        actions = snapshot["allowed_actions"]
        case_no = case["case_no"]
        running = bool(runtime.get("has_running_work"))
        blocking = diagnosis.get("blocking_reason")
        headline = diagnosis.get("headline")
        known = list(diagnosis.get("known") or [])[:3]
        unknown = list(diagnosis.get("unknown") or [])[:2]

        answer = interpretation.get("active_question_answer") or {}
        if answer:
            slot_key = str(answer.get("slot_key") or "")
            state = str(answer.get("state") or "")
            if state == "UNKNOWN_BY_USER":
                prefix = f"明白，{slot_label(slot_key)}我已经记为未知，后面不会重复追问。"
                return prefix + self._short_next(snapshot)
            if state == "UNAVAILABLE":
                prefix = f"明白，{slot_label(slot_key)}目前暂时无法提供，我已经记下，后面不会重复追问。"
                return prefix + self._short_next(snapshot)
            if state == "DECLINED":
                prefix = f"好的，{slot_label(slot_key)}本轮不再要求补充。"
                return prefix + self._short_next(snapshot)
            if state == "ANSWERED" and answer.get("value") not in (None, ""):
                return f"收到，{slot_label(slot_key)}已记录为 {answer.get('value')}。这条信息会作为新的诊断上下文进入下一轮判断。"

        if intent == "CASE_COMPLETION_QUERY":
            if running:
                return (
                    f"Case {case_no} 还在自动分析中，目前有后台任务在运行。"
                    "我不会编一个不可靠的完成时间；任务完成或需要你操作时会直接更新。"
                )
            if case["status"] in {"DIAGNOSED", "ROOT_CAUSE_CONFIRMED", "RESOLVED", "CLOSED"}:
                suffix = f"当前结论：{headline}。" if headline else "当前已经形成可查看的诊断结果。"
                return f"Case {case_no} 当前自动分析已经结束。{suffix}"
            detail = f"当前阻塞点是：{blocking}。" if blocking else "当前没有新的后台任务在运行。"
            recommended = conversation.get("recommended_question") or {}
            if recommended.get("id") == "partial-conclusion":
                return (
                    f"Case {case_no} 当前没有后台任务继续运行。{detail}"
                    "现在已经可以基于现有证据形成阶段结论；如果你要求本轮结束，"
                    "系统会明确列出已确认、尚未确认和后续可选证据。"
                )
            recommended_text = self._recommended_next(snapshot)
            if recommended_text:
                return f"Case {case_no} 当前没有后台任务继续运行。{detail}{recommended_text}"
            choice = actions.get("FINISH_WITH_PARTIAL_CONCLUSION")
            return f"Case {case_no} 当前没有后台任务继续运行。{detail}{choice or '可以按现有证据先形成阶段结论。'}"

        if intent == "CASE_PROGRESS_QUERY":
            user_state = self._user_state_text(snapshot)
            parts = [f"Case {case_no} 当前状态：{user_state}。"]
            parts.append("后台任务：运行中。" if running else "后台任务：无。")
            if headline:
                parts.append(f"阶段结论：{self._clean_item(headline)}。")
            if known:
                parts.append(self._bullet_section("已确认", known))
            if blocking:
                parts.append(self._bullet_section("尚未确认", [blocking]))
            elif unknown:
                parts.append(self._bullet_section("尚未确认", unknown))
            if not running:
                recommended = conversation.get("recommended_question") or {}
                if recommended.get("id") == "partial-conclusion":
                    parts.append("当前没有必须继续追问的信息，可以直接按现有证据形成阶段结论。")
                else:
                    recommended_text = self._recommended_next(snapshot)
                    if recommended_text:
                        parts.append(recommended_text)
                    elif actions.get("FINISH_WITH_PARTIAL_CONCLUSION"):
                        parts.append(str(actions["FINISH_WITH_PARTIAL_CONCLUSION"]))
            return "\n\n".join(part for part in parts if part)

        if intent == "CASE_NEXT_ACTION_QUERY":
            active = conversation.get("active_question") or {}
            if active.get("text"):
                return f"当前如果方便，最有价值的是：{active['text']} 如果暂时拿不到，也可以直接告诉我，我会改走其他路径。"
            if running:
                return "当前没有必须由你补充的信息。系统仍在自动分析；等有新的结论或明确需要现场配合时会直接说明。"

            recommended = conversation.get("recommended_question") or {}
            if recommended.get("text"):
                return (
                    "当前有一项可选但有价值的补充信息："
                    f"{recommended['text']} "
                    f"{recommended.get('fallback') or ''}".strip()
                )
            if recommended.get("id") == "partial-conclusion":
                return self._missing_evidence_summary(snapshot)

            if actions.get("FINISH_WITH_PARTIAL_CONCLUSION"):
                return self._missing_evidence_summary(snapshot)
            return "当前没有必须由你补充的信息，也没有可确定推荐的新证据项。"

        if intent == "CONTROL":
            control = str((interpretation.get("entities") or {}).get("control") or "")
            if control == "FINISH_WITH_PARTIAL_CONCLUSION":
                if running:
                    return (
                        f"已收到结束本轮分析的请求。Case {case_no} 仍保持打开，可随时继续。"
                        "系统不再等待新的外部证据，也不会把缺失信息继续作为重复追问条件。"
                        "当前已启动的分析任务完成后，只基于现有证据形成阶段结论；"
                        "不会因此标记 Root Cause Confirmed、Resolved 或 Fix Verified。"
                    )
                return self._partial_conclusion(
                    snapshot,
                    prefix=f"已结束本轮主动分析；Case {case_no} 仍保持打开，可随时继续。",
                )
            if control == "CONTINUE_ANALYSIS":
                if running:
                    return (
                        f"已恢复 Case {case_no} 的分析状态。当前已有后台任务在运行，"
                        "无需重复触发，也不需要你重复补充已有信息。"
                    )
                recommended = self._recommended_next(snapshot)
                if recommended:
                    return f"已恢复 Case {case_no} 的分析状态。{recommended}"
                return (
                    f"已恢复 Case {case_no} 的分析状态。当前没有必须由你补充的信息，"
                    "也没有新的后台任务需要重复启动；后续有新的直接证据时可以继续分析。"
                )

        if intent == "HYBRID_KNOWLEDGE_DIAGNOSIS":
            return (
                f"另外，你这句话里包含了当前 Case {case_no} 的现场异常信息，这部分会作为新的诊断上下文记录，"
                "系统会据此重新判断；知识解释本身不会被当成故障证据。"
            )

        if interpretation.get("material_diagnostic_context"):
            return f"收到，这条补充已经作为 Case {case_no} 的新诊断上下文记录，系统会据此重新判断；普通聊天不会被计入诊断循环。"
        return self._short_next(snapshot)

    @staticmethod
    def _user_state_text(snapshot: dict[str, Any]) -> str:
        case = snapshot.get("case") or {}
        runtime = snapshot.get("runtime") or {}
        conversation = snapshot.get("conversation") or {}
        status = str(case.get("status") or "")
        if runtime.get("has_running_work"):
            return "自动分析中"
        if status == "WAITING_USER":
            recommended = conversation.get("recommended_question") or {}
            if recommended.get("id") == "partial-conclusion":
                return "本轮分析已完成，可形成阶段结论"
            return "等待补充信息"
        mapping = {
            "NEW": "已建 Case，等待开始分析",
            "ANALYZING": "当前无后台任务，可继续分析",
            "DIAGNOSED": "已形成诊断结论",
            "ROOT_CAUSE_CONFIRMED": "根因已确认",
            "RESOLVED": "问题已解决",
            "CLOSED": "Case 已关闭",
            "FAILED": "分析异常，需检查任务状态",
        }
        return mapping.get(status, status or "状态未知")

    @classmethod
    def _optional_evidence_actions(cls, snapshot: dict[str, Any]) -> list[str]:
        actions = snapshot.get("allowed_actions") or {}
        keys = ("UPLOAD_RECORDING", "UPLOAD_PCAP", "REPRODUCE_WHEN_AVAILABLE")
        return [cls._clean_item(actions[key]) for key in keys if actions.get(key)]

    @classmethod
    def _missing_evidence_summary(cls, snapshot: dict[str, Any]) -> str:
        diagnosis = snapshot.get("diagnosis") or {}
        known = list(diagnosis.get("known") or [])[:3]
        unknown = list(diagnosis.get("unknown") or [])[:3]
        blocking = str(diagnosis.get("blocking_reason") or "").strip()
        optional = cls._optional_evidence_actions(snapshot)

        sections = [
            "当前没有必须由你补充的信息。现在可以直接基于现有证据形成阶段结论。"
        ]
        known_section = cls._bullet_section("已确认", known)
        if known_section:
            sections.append(known_section)
        unresolved = unknown or ([blocking] if blocking else [])
        unresolved_section = cls._bullet_section("尚未确认", unresolved)
        if unresolved_section:
            sections.append(unresolved_section)
        if optional:
            sections.append(cls._bullet_section("后续可选", optional))
        else:
            sections.append("后续可选\n• 当前没有可确定推荐的新证据项")
        return "\n\n".join(section for section in sections if section)

    @classmethod
    def _partial_conclusion(cls, snapshot: dict[str, Any], *, prefix: str = "") -> str:
        case = snapshot.get("case") or {}
        diagnosis = snapshot.get("diagnosis") or {}
        runtime = snapshot.get("runtime") or {}
        headline = str(diagnosis.get("headline") or "").strip()
        known = list(diagnosis.get("known") or [])[:5]
        unknown = list(diagnosis.get("unknown") or [])[:5]
        blocking = str(diagnosis.get("blocking_reason") or "").strip()
        optional = cls._optional_evidence_actions(snapshot)

        sections: list[str] = []
        if prefix:
            sections.append(prefix)
        if headline:
            sections.append(f"当前阶段结论：{cls._clean_item(headline)}。")
        else:
            sections.append("当前阶段结论：现有证据不足以形成更强的正式根因结论。")

        known_section = cls._bullet_section("已确认", known)
        if known_section:
            sections.append(known_section)

        unresolved = unknown or ([blocking] if blocking else [])
        unresolved_section = cls._bullet_section("尚未确认", unresolved)
        if unresolved_section:
            sections.append(unresolved_section)

        if runtime.get("has_running_work"):
            sections.append("后台状态：仍有已启动任务；任务完成后会按同一证据边界更新阶段结论。")

        if optional:
            sections.append(cls._bullet_section("后续可选", optional))
        else:
            sections.append("后续可选\n• 当前没有可确定推荐的新证据项")

        if case.get("status") not in {"ROOT_CAUSE_CONFIRMED", "RESOLVED"}:
            sections.append(
                "边界说明：以上仅为阶段结论，不等于 Root Cause Confirmed，"
                "也不表示问题已 Resolved 或 Fix Verified。"
            )
        return "\n\n".join(section for section in sections if section)

    @staticmethod
    def _recommended_next(snapshot: dict[str, Any]) -> str:
        conversation = snapshot.get("conversation") or {}
        recommended = conversation.get("recommended_question") or {}
        text = str(recommended.get("text") or "").strip()
        fallback = str(recommended.get("fallback") or "").strip()
        if text:
            return f"下一条最有价值的信息是：{text}" + (f" {fallback}" if fallback else "")
        if fallback:
            return fallback
        return ""

    @classmethod
    def _short_next(cls, snapshot: dict[str, Any]) -> str:
        runtime = snapshot.get("runtime") or {}
        diagnosis = snapshot.get("diagnosis") or {}
        actions = snapshot.get("allowed_actions") or {}
        if runtime.get("has_running_work"):
            return "当前自动任务仍在运行，有新发现会直接更新。"
        recommended = snapshot.get("conversation", {}).get("recommended_question") or {}
        if recommended.get("id") == "partial-conclusion":
            return cls._partial_conclusion(snapshot)
        recommended_text = cls._recommended_next(snapshot)
        if recommended_text:
            return recommended_text
        if diagnosis.get("blocking_reason"):
            partial = actions.get("FINISH_WITH_PARTIAL_CONCLUSION")
            return f"当前阻塞点是：{diagnosis['blocking_reason']}。{partial or ''}"
        return "当前没有新的必须操作；如果证据不足，会明确说明能确认什么、不能确认什么。"
