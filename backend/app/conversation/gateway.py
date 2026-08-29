from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings
from app.diagnosis.gateway import assert_gateway_payload_safe, redact_gateway_value


class ConversationGatewayError(RuntimeError):
    pass


class ConversationGatewayClient:
    """Reasoning Gateway client for semantic turn interpretation/response planning.

    The gateway never receives raw Evidence payload bytes or credentials and its
    outputs are non-executing contracts. Device action authority remains in the
    deterministic Policy/Registry/Orchestrator path.
    """

    turn_prompt_version = "voip-conversation-turn-v1"
    response_prompt_version = "voip-conversation-response-plan-v1"

    def __init__(self, url: str | None = None, token: str | None = None, model: str | None = None):
        self.url = settings.reasoning_gateway_url if url is None else url
        self.token = settings.reasoning_gateway_token if token is None else token
        self.model = settings.reasoning_gateway_model if model is None else model

    def enabled(self) -> bool:
        return bool(self.url)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled():
            raise ConversationGatewayError("CONVERSATION_GATEWAY_NOT_CONFIGURED")
        payload = redact_gateway_value(payload)
        assert_gateway_payload_safe(payload)
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            with httpx.Client(timeout=settings.reasoning_gateway_timeout_seconds) as client:
                response = client.post(self.url, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            raise ConversationGatewayError(f"CONVERSATION_GATEWAY_FAILED:{type(exc).__name__}") from exc
        if not isinstance(data, dict):
            raise ConversationGatewayError("CONVERSATION_GATEWAY_INVALID_RESPONSE")
        return data

    def interpret_turn(
        self,
        *,
        text: str,
        attachments: list[dict[str, Any]],
        active_question: dict[str, Any] | None,
        slots: dict[str, Any],
        case_context: dict[str, Any] | None,
        deterministic_candidate: dict[str, Any],
        conversation_entities: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "schema_version": "voip-conversation-turn-gateway-v1",
            "prompt_version": self.turn_prompt_version,
            "model": self.model,
            "message": {
                "text": (text or "")[:4000],
                "attachments": [
                    {
                        "attachment_id": str(item.get("attachment_id") or item.get("file_key") or f"attachment-{i+1}"),
                        "filename": str(item.get("filename") or "")[:256],
                        "message_type": str(item.get("message_type") or "")[:32],
                    }
                    for i, item in enumerate(attachments[:32])
                ],
            },
            "conversation": {
                "active_question": active_question,
                "slots": slots,
                "entities": dict(conversation_entities or {}),
                "case_context": case_context,
            },
            "deterministic_candidate": deterministic_candidate,
            "policy": {
                "output_schema": "conversation-turn-v1",
                "output_is_non_executing": True,
                "active_question_has_priority": True,
                "knowledge_interruption_may_preempt_active_question": True,
                "chat_only_must_not_become_diagnostic_evidence": True,
                "knowledge_in_case_must_not_advance_diagnosis": True,
                "raw_commands_forbidden": True,
                "root_cause_confirmation_forbidden": True,
                "case_override_forbidden": True,
            },
        }
        data = self._post(payload)
        proposal = data.get("proposal", data)
        if not isinstance(proposal, dict):
            raise ConversationGatewayError("CONVERSATION_TURN_PROPOSAL_INVALID")
        return {
            "proposal": proposal,
            "model": str(data.get("model") or self.model or ""),
            "prompt_version": str(data.get("prompt_version") or self.turn_prompt_version),
        }

    def plan_response(self, *, snapshot: dict[str, Any], intent: str) -> dict[str, Any]:
        """Let the model select grounded catalog IDs; it cannot author facts."""
        payload = {
            "schema_version": "voip-conversation-response-gateway-v1",
            "prompt_version": self.response_prompt_version,
            "model": self.model,
            "intent": intent,
            "snapshot": snapshot,
            "policy": {
                "output_schema": "conversation-response-plan-v1",
                "selection_only": True,
                "only_catalog_ids_allowed": True,
                "no_new_facts": True,
                "no_eta_without_snapshot_telemetry": True,
                "no_device_execution": True,
                "max_questions": 1,
            },
        }
        data = self._post(payload)
        proposal = data.get("proposal", data.get("response_plan", data))
        if not isinstance(proposal, dict):
            raise ConversationGatewayError("CONVERSATION_RESPONSE_PLAN_INVALID")
        return {
            "proposal": proposal,
            "model": str(data.get("model") or self.model or ""),
            "prompt_version": str(data.get("prompt_version") or self.response_prompt_version),
        }
