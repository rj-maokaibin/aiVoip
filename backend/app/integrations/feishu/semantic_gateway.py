from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings
from app.diagnosis.gateway import assert_gateway_payload_safe, redact_gateway_value


class SemanticGatewayError(RuntimeError):
    pass


class SemanticGatewayClient:
    """Reasoning Gateway client dedicated to AI1 semantic extraction.

    Only normalized message/context metadata is sent. Output is explicitly a
    non-executing proposal and can never authorize or execute a Case operation.
    """

    prompt_version = "feishu-semantic-router-v1"

    def __init__(self, url: str | None = None, token: str | None = None, model: str | None = None):
        self.url = settings.reasoning_gateway_url if url is None else url
        self.token = settings.reasoning_gateway_token if token is None else token
        self.model = settings.reasoning_gateway_model if model is None else model

    def enabled(self) -> bool:
        return bool(self.url)

    def resolve(
        self,
        *,
        text: str,
        attachments: list[dict[str, Any]],
        deterministic: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.enabled():
            raise SemanticGatewayError("SEMANTIC_GATEWAY_NOT_CONFIGURED")
        try:
            payload = {
                "schema_version": "feishu-semantic-gateway-v1",
                "prompt_version": self.prompt_version,
                "model": self.model,
                "message": {
                    "text": text[:4000],
                    "attachments": [
                        {
                            "attachment_id": str(item.get("attachment_id") or item.get("file_key") or f"attachment-{i+1}"),
                            "filename": str(item.get("filename") or "")[:256],
                            "message_type": str(item.get("message_type") or "")[:32],
                        }
                        for i, item in enumerate(attachments[:32])
                    ],
                },
                "deterministic_candidate": deterministic,
                "context": context,
                "policy": {
                    "output_schema": "feishu-semantic-intent-v1",
                    "output_is_non_executing_proposal": True,
                    "raw_commands_forbidden": True,
                    "case_override_forbidden": True,
                    "rbac_and_policy_recheck_required": True,
                    "root_cause_confirmation_forbidden": True,
                    "deterministic_router_remains_execution_authority": True,
                },
            }
            payload = redact_gateway_value(payload)
            assert_gateway_payload_safe(payload)
            headers = {"Content-Type": "application/json"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            with httpx.Client(timeout=settings.reasoning_gateway_timeout_seconds) as client:
                response = client.post(self.url, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
        except SemanticGatewayError:
            raise
        except Exception as exc:
            raise SemanticGatewayError(f"SEMANTIC_GATEWAY_FAILED:{type(exc).__name__}") from exc
        if not isinstance(data, dict):
            raise SemanticGatewayError("SEMANTIC_GATEWAY_INVALID_RESPONSE")
        proposal = data.get("proposal", data)
        if not isinstance(proposal, dict):
            raise SemanticGatewayError("SEMANTIC_GATEWAY_PROPOSAL_INVALID")
        return {
            "proposal": proposal,
            "model": str(data.get("model") or self.model or ""),
            "prompt_version": str(data.get("prompt_version") or self.prompt_version),
        }
