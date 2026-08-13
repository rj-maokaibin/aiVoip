from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.enums import DiagnosticQuestionLevel, DiagnosticQuestionState, EventType
from app.core.errors import AppError
from app.db.models import CausalAssessment, DiagnosticQuestion, Evidence
from app.services.audit import audit
from app.services.events import emit_event


def _canonical(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class RequiredEvidenceSpec(BaseModel):
    must_findings: list[str] = Field(default_factory=list)
    min_level: str = "L1"


class DiagnosticQuestionTemplate(BaseModel):
    id: str
    version: str = "1.0.0"
    level: DiagnosticQuestionLevel
    title: str
    priority: int = 100
    information_gain: int = 1000
    required_evidence: RequiredEvidenceSpec = Field(default_factory=RequiredEvidenceSpec)
    next_questions: list[str] = Field(default_factory=list)
    next_by_route: dict[str, list[str]] = Field(default_factory=dict)
    experiment_profiles: list[str] = Field(default_factory=list)

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @property
    def checksum(self) -> str:
        return hashlib.sha256(_canonical(self.canonical()).encode()).hexdigest()


class DiagnosticQuestionRegistry:
    """Versioned, declarative DiagnosticQuestion DAG registry.

    The registry contains no executable expressions. It validates references and rejects
    cycles so a Coding/LLM agent cannot invent runtime question semantics.
    """

    def __init__(self, root: str | Path | None = None):
        root = Path(root or Path(__file__).resolve().parents[3] / "profiles" / "questions")
        self.root = root
        self._templates: dict[str, DiagnosticQuestionTemplate] = {}
        self.reload()

    def reload(self) -> None:
        rows: dict[str, DiagnosticQuestionTemplate] = {}
        for path in sorted(self.root.glob("*.yaml")):
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for raw in payload.get("questions") or []:
                item = DiagnosticQuestionTemplate.model_validate(raw)
                if item.id in rows:
                    raise ValueError(f"DUPLICATE_DIAGNOSTIC_QUESTION:{item.id}")
                rows[item.id] = item
        if not rows:
            raise ValueError("DIAGNOSTIC_QUESTION_REGISTRY_EMPTY")
        for item in rows.values():
            refs = list(item.next_questions)
            for targets in item.next_by_route.values():
                refs.extend(targets)
            missing = [x for x in refs if x not in rows]
            if missing:
                raise ValueError(f"DIAGNOSTIC_QUESTION_REFERENCE_MISSING:{item.id}:{','.join(missing)}")
        self._assert_dag(rows)
        self._templates = rows

    @staticmethod
    def _assert_dag(rows: dict[str, DiagnosticQuestionTemplate]) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visited:
                return
            if key in visiting:
                raise ValueError(f"DIAGNOSTIC_QUESTION_CYCLE:{key}")
            visiting.add(key)
            item = rows[key]
            refs = list(item.next_questions)
            for targets in item.next_by_route.values():
                refs.extend(targets)
            for nxt in refs:
                visit(nxt)
            visiting.remove(key)
            visited.add(key)

        for key in rows:
            visit(key)

    def get(self, question_key: str) -> DiagnosticQuestionTemplate:
        try:
            return self._templates[question_key]
        except KeyError as exc:
            raise AppError("DIAGNOSTIC_QUESTION_TEMPLATE_NOT_FOUND", details={"question_key": question_key}) from exc

    def list(self) -> list[DiagnosticQuestionTemplate]:
        return [self._templates[x] for x in sorted(self._templates)]


class DiagnosticQuestionGraph:
    def __init__(self, registry: DiagnosticQuestionRegistry | None = None):
        self.registry = registry or DiagnosticQuestionRegistry()

    def ensure_question(
        self,
        db: Session,
        *,
        case_id: str,
        question_key: str,
        session_id: str | None = None,
        parent_question_id: str | None = None,
        state: DiagnosticQuestionState = DiagnosticQuestionState.OPEN,
        selected_reason: str | None = None,
    ) -> DiagnosticQuestion:
        template = self.registry.get(question_key)
        query = select(DiagnosticQuestion).where(
            DiagnosticQuestion.case_id == case_id,
            DiagnosticQuestion.question_key == question_key,
        )
        if session_id is None:
            query = query.where(DiagnosticQuestion.session_id.is_(None))
        else:
            query = query.where(DiagnosticQuestion.session_id == session_id)
        row = db.scalar(query.order_by(DiagnosticQuestion.created_at.desc()))
        if row:
            return row
        row = DiagnosticQuestion(
            case_id=case_id,
            session_id=session_id,
            parent_question_id=parent_question_id,
            question_key=template.id,
            template_version=template.version,
            template_checksum=template.checksum,
            state=state.value,
            level=template.level.value,
            priority=template.priority,
            information_gain=template.information_gain,
            selected_reason=selected_reason,
            requirements_json=template.required_evidence.model_dump(mode="json"),
        )
        db.add(row)
        db.flush()
        emit_event(
            db,
            event_type=EventType.DIAGNOSTIC_QUESTION_CHANGED,
            case_id=case_id,
            entity_type="diagnostic_question",
            entity_id=row.id,
            payload={"state": row.state, "question_key": row.question_key, "level": row.level},
        )
        return row

    @staticmethod
    def _level_rank(value: str | None) -> int:
        try:
            raw = str(value or "").upper()
            if raw.startswith("L"):
                return int(raw[1:])
        except (TypeError, ValueError):
            pass
        return 999

    def _validate_answer_requirements(
        self,
        db: Session,
        *,
        question: DiagnosticQuestion,
        template: DiagnosticQuestionTemplate,
        answer: dict[str, Any],
        evidence_refs: list[dict[str, Any]],
    ) -> None:
        required = template.required_evidence
        findings = set(answer.get("findings") or answer.get("satisfied_findings") or [])
        missing = sorted(set(required.must_findings) - findings)
        if missing:
            raise AppError(
                "DIAGNOSTIC_QUESTION_EVIDENCE_INSUFFICIENT",
                details={"question_key": question.question_key, "missing_findings": missing},
            )

        observed_levels: list[str] = []
        invalid_refs: list[dict[str, Any]] = []
        for ref in evidence_refs:
            if ref.get("evidence_id"):
                row = db.get(Evidence, ref["evidence_id"])
                if not row or row.case_id != question.case_id:
                    invalid_refs.append({"evidence_id": ref.get("evidence_id")})
                    continue
                observed_levels.append(row.level)
                continue
            if ref.get("ref_type") == "CAUSAL_ASSESSMENT" and ref.get("ref_id"):
                row = db.get(CausalAssessment, ref["ref_id"])
                if not row or row.case_id != question.case_id:
                    invalid_refs.append({"ref_type": ref.get("ref_type"), "ref_id": ref.get("ref_id")})
                    continue
                observed_levels.append("L1")
                continue
            # Unknown references are retained for audit but do not satisfy deterministic evidence gates.
            invalid_refs.append(dict(ref))

        required_rank = self._level_rank(required.min_level)
        level_ok = any(self._level_rank(level) <= required_rank for level in observed_levels)
        if not level_ok:
            raise AppError(
                "DIAGNOSTIC_QUESTION_EVIDENCE_INSUFFICIENT",
                details={
                    "question_key": question.question_key,
                    "required_min_level": required.min_level,
                    "observed_levels": observed_levels,
                    "invalid_refs": invalid_refs,
                },
            )

    def answer(
        self,
        db: Session,
        *,
        question: DiagnosticQuestion,
        answer: dict[str, Any],
        evidence_refs: list[dict[str, Any]] | None = None,
        route: str | None = None,
        actor: str | None = None,
    ) -> list[DiagnosticQuestion]:
        template = self.registry.get(question.question_key)
        refs = list(evidence_refs or [])
        self._validate_answer_requirements(db, question=question, template=template, answer=answer, evidence_refs=refs)
        question.state = DiagnosticQuestionState.ANSWERED.value
        question.answer_json = dict(answer)
        question.evidence_refs_json = refs
        targets = list(template.next_questions)
        if route:
            targets.extend(template.next_by_route.get(str(route).upper(), []))
        created = [
            self.ensure_question(
                db,
                case_id=question.case_id,
                question_key=target,
                parent_question_id=question.id,
                selected_reason=f"parent_answered:{question.question_key}",
            )
            for target in dict.fromkeys(targets)
        ]
        audit(
            db,
            case_id=question.case_id,
            actor=actor,
            event_type=EventType.DIAGNOSTIC_QUESTION_CHANGED.value,
            action="DIAGNOSTIC_QUESTION_ANSWER",
            target_type="diagnostic_question",
            target_id=question.id,
            detail={"question_key": question.question_key, "answer": answer, "route": route, "next": [x.question_key for x in created]},
        )
        emit_event(
            db,
            event_type=EventType.DIAGNOSTIC_QUESTION_CHANGED,
            case_id=question.case_id,
            entity_type="diagnostic_question",
            entity_id=question.id,
            payload={"state": question.state, "question_key": question.question_key, "next": [x.question_key for x in created]},
        )
        db.flush()
        return created

    def select_next(self, db: Session, *, case_id: str, actor: str | None = None) -> DiagnosticQuestion | None:
        rows = list(
            db.scalars(
                select(DiagnosticQuestion).where(
                    DiagnosticQuestion.case_id == case_id,
                    DiagnosticQuestion.state.in_([DiagnosticQuestionState.OPEN.value, DiagnosticQuestionState.IN_PROGRESS.value]),
                )
            )
        )
        eligible: list[DiagnosticQuestion] = []
        for row in rows:
            if row.parent_question_id:
                parent = db.get(DiagnosticQuestion, row.parent_question_id)
                if not parent or parent.state != DiagnosticQuestionState.ANSWERED.value:
                    continue
            eligible.append(row)
        if not eligible:
            return None
        eligible.sort(key=lambda x: (-int(x.information_gain), int(x.priority), x.created_at, x.id))
        selected = eligible[0]
        if selected.state == DiagnosticQuestionState.OPEN.value:
            selected.state = DiagnosticQuestionState.IN_PROGRESS.value
            selected.selected_reason = selected.selected_reason or "highest_information_gain"
            emit_event(
                db,
                event_type=EventType.DIAGNOSTIC_QUESTION_CHANGED,
                case_id=case_id,
                entity_type="diagnostic_question",
                entity_id=selected.id,
                payload={"state": selected.state, "question_key": selected.question_key, "reason": selected.selected_reason},
            )
            db.flush()
        return selected

    def sync_reproduction_answer(
        self,
        db: Session,
        *,
        session_id: str,
        sufficient: bool,
        findings: list[str],
        evidence_ids: list[str],
        actor: str | None = None,
    ) -> list[DiagnosticQuestion]:
        question = db.scalar(
            select(DiagnosticQuestion)
            .where(DiagnosticQuestion.session_id == session_id)
            .order_by(DiagnosticQuestion.created_at.desc())
        )
        if not question or not sufficient or question.state == DiagnosticQuestionState.ANSWERED.value:
            return []
        return self.answer(
            db,
            question=question,
            answer={"result": "SUFFICIENT", "findings": sorted(set(findings))},
            evidence_refs=[{"evidence_id": x, "level": "L1"} for x in dict.fromkeys(evidence_ids)],
            actor=actor,
        )

    def candidate_experiments(self, question_key: str) -> list[str]:
        return list(self.registry.get(question_key).experiment_profiles)
