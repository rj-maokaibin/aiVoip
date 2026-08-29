from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.conversation.contracts import ResponsePlan
from app.conversation.gateway import ConversationGatewayClient, ConversationGatewayError
from app.conversation.snapshot import ConversationSnapshotBuilder
from app.conversation.state_service import slot_label
from app.core.config import settings


class GroundedConversationResponder:
    """Render natural Chinese replies from a deterministic truth catalog.

    The optional LLM only selects catalog IDs.  All user-visible technical facts
    are resolved from the snapshot so a fluent response cannot invent analyzer or
    product facts.
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
        snapshot = self.snapshot_builder.build(db, case_id)
        plan = self._plan(snapshot=snapshot, intent=intent)
        if plan is not None:
            rendered = self._render_plan(snapshot, plan)
            if rendered:
                return rendered
        return self._deterministic_render(snapshot, intent, interpretation or {})

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
            choice = actions.get("FINISH_WITH_PARTIAL_CONCLUSION")
            return f"Case {case_no} 当前没有后台任务继续运行。{detail}{choice or '可以按现有证据先形成阶段结论。'}"

        if intent == "CASE_PROGRESS_QUERY":
            state_text = "仍有后台任务在运行" if running else "当前没有后台任务在运行"
            parts = [f"Case {case_no} 当前状态：{case['status']}，{state_text}。"]
            if headline:
                parts.append(f"阶段结论：{headline}。")
            if known:
                parts.append("已经确认：" + "；".join(str(x) for x in known) + "。")
            if blocking:
                parts.append(f"当前阻塞点：{blocking}。")
            elif unknown:
                parts.append("仍未确认：" + "；".join(str(x) for x in unknown) + "。")
            if not running and actions.get("FINISH_WITH_PARTIAL_CONCLUSION"):
                parts.append(actions["FINISH_WITH_PARTIAL_CONCLUSION"])
            return "".join(parts)

        if intent == "CASE_NEXT_ACTION_QUERY":
            active = conversation.get("active_question") or {}
            if active.get("text"):
                return f"当前如果方便，最有价值的是：{active['text']} 如果暂时拿不到，也可以直接告诉我，我会改走其他路径。"
            if running:
                return "当前不需要你额外操作，系统仍在自动分析。等有新的结论或明确需要现场配合时，我会直接说明。"
            if actions.get("FINISH_WITH_PARTIAL_CONCLUSION"):
                return "当前没有必须补充的操作。你可以继续补充新的直接证据，也可以按现有证据先形成阶段结论。"
            return "当前没有需要你立即处理的动作；我会按现有证据状态继续给出下一步。"

        if intent == "CONTROL":
            control = str((interpretation.get("entities") or {}).get("control") or "")
            if control == "FINISH_WITH_PARTIAL_CONCLUSION":
                return "收到。我会按现有证据收敛本轮分析，不再把缺失但暂时无法获得的信息作为重复追问条件。"
            if control == "CONTINUE_ANALYSIS":
                if running:
                    return "收到，继续按当前证据推进；现在已有后台任务在运行，不需要重复触发。"
                return "收到，继续按当前证据推进。如果没有新增有效证据，我会明确给出阶段结论和仍未确认的边界。"

        if interpretation.get("material_diagnostic_context"):
            return f"收到，这条补充已经作为 Case {case_no} 的新诊断上下文记录，系统会据此重新判断；不会把普通聊天当成诊断循环。"
        return self._short_next(snapshot)

    @staticmethod
    def _short_next(snapshot: dict[str, Any]) -> str:
        runtime = snapshot.get("runtime") or {}
        diagnosis = snapshot.get("diagnosis") or {}
        actions = snapshot.get("allowed_actions") or {}
        if runtime.get("has_running_work"):
            return "我会继续等当前自动任务完成，有新发现会直接更新。"
        if diagnosis.get("blocking_reason"):
            partial = actions.get("FINISH_WITH_PARTIAL_CONCLUSION")
            return f"当前阻塞点是：{diagnosis['blocking_reason']}。{partial or ''}"
        return "我会继续按现有证据推进；如果证据不足，会明确说明能确认什么、不能确认什么。"
