from __future__ import annotations

from celery.utils.log import get_task_logger

from app.workers.celery_app import celery_app


log = get_task_logger(__name__)


@celery_app.task(name="conversation.ingest_knowledge_turn", bind=True, autoretry_for=(), max_retries=0)
def ingest_knowledge_turn(
    self,
    text: str,
    source_context: dict | None = None,
    attachments: list[dict] | None = None,
):
    """Persist and answer a no-Case knowledge turn.

    This task never creates a Case or technical Evidence. It is the continuity
    path for product/protocol/configuration conversations before a fault exists.
    """
    from app.conversation.orchestrator import AssistantConversationOrchestrator
    from app.db.session import SessionLocal
    from app.integrations.feishu.feedback import enqueue_reply

    source_context = source_context or {}
    message_id = str(source_context.get("message_id") or "")
    db = SessionLocal()
    try:
        result = AssistantConversationOrchestrator().prepare_turn(
            db,
            text=(text or "").strip(),
            source_context=source_context,
            case_id=None,
            case_context=None,
            attachments=attachments or [],
        )
        db.commit()
        if result.response_text:
            enqueue_reply(message_id, result.response_text)
        return {
            "status": "OK",
            "conversation_id": result.conversation_id,
            "conversation_turn_id": result.turn_id,
            "intent": result.interpretation.get("intent"),
            "material_diagnostic_context": result.material_diagnostic_context,
            "case_created": False,
            "evidence_id": None,
        }
    except Exception as exc:
        db.rollback()
        log.exception("knowledge conversation turn failed message=%s", message_id)
        return {
            "status": "FAILED",
            "reason": f"{type(exc).__name__}:{exc}",
            "case_created": False,
        }
    finally:
        db.close()
