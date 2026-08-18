from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings
from app.diagnosis.gateway import assert_gateway_payload_safe, redact_gateway_value


class CopilotGatewayError(RuntimeError):
    pass


_DEVICE_INFO_ALLOWLIST = {
    "product",
    "model",
    "version",
    "software_version",
    "firmware_version",
    "hardware_version",
    "platform",
}


def _safe_device_info(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if str(key) in _DEVICE_INFO_ALLOWLIST
    }


def compact_copilot_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Keep only current-Case evidence semantics needed for Q&A.

    Direct device access identifiers and raw analyzer payloads are intentionally
    excluded even for engineering roles. Device metadata is allowlisted before
    generic redaction is applied, so secrets/credentials cannot rely on regex
    detection alone. Evidence IDs remain because every claim must cite exact
    current-Case Evidence and pass ClaimGroundingValidator.
    """
    case = snapshot.get("case") or {}
    preliminary = snapshot.get("preliminary_report") or None
    return {
        "schema_version": snapshot.get("schema_version"),
        "case": {
            "summary": case.get("summary"),
            "status": case.get("status"),
            "case_no": case.get("case_no"),
        },
        "viewer_role": snapshot.get("viewer_role"),
        "raw_evidence_visible_to_requester": bool(snapshot.get("raw_evidence_visible")),
        "devices": [
            {
                "alias": f"device_{index + 1}",
                "platform_id": item.get("platform_id"),
                "device_info": _safe_device_info(item.get("device_info")),
            }
            for index, item in enumerate((snapshot.get("devices") or [])[:10])
        ],
        "evidences": [
            {
                "id": item.get("id"),
                "type": item.get("type"),
                "kind": item.get("kind"),
                "scope": item.get("scope"),
                "level": item.get("level"),
                "completeness": item.get("completeness"),
            }
            for item in (snapshot.get("evidences") or [])[:200]
        ],
        "analyzers": {
            name: {
                "run_id": item.get("run_id"),
                "status": item.get("status"),
                "version": item.get("version"),
                "summary": item.get("summary") or {},
                "input_evidence_ids": item.get("input_evidence_ids") or [],
            }
            for name, item in (snapshot.get("analyzers") or {}).items()
        },
        "preliminary_report": preliminary,
        "diagnosis": snapshot.get("diagnosis"),
        "reproductions": (snapshot.get("reproductions") or [])[:20],
        "experiments": (snapshot.get("experiments") or [])[:20],
        "fix_verifications": (snapshot.get("fix_verifications") or [])[:20],
        "authority": snapshot.get("authority") or {},
        "fingerprint": snapshot.get("fingerprint"),
    }


class CaseCopilotGatewayClient:
    prompt_version = "ai-case-copilot-v1"

    def __init__(self, url: str | None = None, token: str | None = None, model: str | None = None):
        self.url = settings.reasoning_gateway_url if url is None else url
        self.token = settings.reasoning_gateway_token if token is None else token
        self.model = settings.reasoning_gateway_model if model is None else model

    def enabled(self) -> bool:
        return bool(self.url)

    def answer(self, *, question: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled():
            raise CopilotGatewayError("COPILOT_GATEWAY_NOT_CONFIGURED")
        try:
            payload = {
                "schema_version": "ai-case-copilot-gateway-v1",
                "prompt_version": self.prompt_version,
                "model": self.model,
                "question": question[:4000],
                "case_snapshot": compact_copilot_snapshot(snapshot),
                "policy": {
                    "output_schema": "ai-case-copilot-v1",
                    "read_only": True,
                    "current_case_only": True,
                    "every_diagnostic_claim_must_cite_current_case_evidence": True,
                    "claims_are_l5_proposed_only": True,
                    "root_cause_confirmation_forbidden": True,
                    "evidence_level_promotion_forbidden": True,
                    "raw_device_commands_forbidden": True,
                    "control_actions_must_return_control_intent_required": True,
                    "execution_authority": "DETERMINISTIC_ROUTER_RBAC_POLICY_ORCHESTRATOR",
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
        except Exception as exc:
            raise CopilotGatewayError(f"COPILOT_GATEWAY_FAILED:{type(exc).__name__}") from exc
        if not isinstance(data, dict):
            raise CopilotGatewayError("COPILOT_GATEWAY_INVALID_RESPONSE")
        proposal = data.get("proposal", data)
        if not isinstance(proposal, dict):
            raise CopilotGatewayError("COPILOT_GATEWAY_PROPOSAL_INVALID")
        return {
            "proposal": proposal,
            "model": str(data.get("model") or self.model or ""),
            "prompt_version": str(data.get("prompt_version") or self.prompt_version),
        }
