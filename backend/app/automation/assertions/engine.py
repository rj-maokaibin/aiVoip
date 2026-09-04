from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.automation.assertions.operators import AssertionOperatorError, evaluate_operator
from app.automation.assertions.resolver import MISSING, NormalizedEvidenceStore, resolve_path
from app.automation.contracts import AssertionSpec, TestVerdict


def _expand(value: Any, parameters: dict[str, Any]) -> Any:
    if isinstance(value, str):
        if value.startswith("${") and value.endswith("}") and value.count("${") == 1:
            key = value[2:-1]
            return parameters.get(key, value)
        rendered = value
        for key, replacement in parameters.items():
            rendered = rendered.replace("${" + key + "}", str(replacement))
        return rendered
    if isinstance(value, list):
        return [_expand(item, parameters) for item in value]
    if isinstance(value, tuple):
        return tuple(_expand(item, parameters) for item in value)
    if isinstance(value, dict):
        return {key: _expand(child, parameters) for key, child in value.items()}
    return value


@dataclass(frozen=True)
class AssertionResultData:
    assertion_id: str
    source: str
    path: str
    operator: str
    expected: Any
    actual: Any
    verdict: TestVerdict
    evidence_refs: tuple[str, ...]
    source_timestamp: str | None
    route: dict[str, Any] | None
    reason: str | None = None


@dataclass(frozen=True)
class AssertionEvaluation:
    verdict: TestVerdict
    results: tuple[AssertionResultData, ...]
    reason: str | None = None


class AssertionEngine:
    """The sole deterministic Test verdict oracle in V1."""

    def evaluate(
        self,
        assertions: tuple[AssertionSpec, ...],
        evidence: NormalizedEvidenceStore,
        *,
        parameters: dict[str, Any] | None = None,
        blocked_reason: str | None = None,
        inconclusive_reason: str | None = None,
        cleanup_verified: bool = True,
    ) -> AssertionEvaluation:
        if blocked_reason:
            return AssertionEvaluation(TestVerdict.BLOCKED, (), blocked_reason)
        parameters = dict(parameters or {})
        results: list[AssertionResultData] = []
        saw_fail = False
        saw_inconclusive = False

        for spec in assertions:
            envelope = evidence.get(spec.source)
            expected = _expand(spec.expected, parameters)
            if envelope is None:
                results.append(AssertionResultData(
                    assertion_id=spec.assertion_id,
                    source=spec.source,
                    path=spec.path,
                    operator=spec.operator,
                    expected=expected,
                    actual=None,
                    verdict=TestVerdict.INCONCLUSIVE,
                    evidence_refs=(),
                    source_timestamp=None,
                    route=None,
                    reason="EVIDENCE_SOURCE_MISSING",
                ))
                saw_inconclusive = True
                continue
            actual = resolve_path(envelope.data, spec.path)
            try:
                passed = evaluate_operator(spec.operator, actual, expected)
                verdict = TestVerdict.PASS if passed else TestVerdict.FAIL
                reason = None if passed else "ASSERTION_FALSE"
            except AssertionOperatorError as exc:
                verdict = TestVerdict.INCONCLUSIVE
                reason = str(exc)
            if verdict == TestVerdict.FAIL:
                saw_fail = True
            elif verdict == TestVerdict.INCONCLUSIVE:
                saw_inconclusive = True
            results.append(AssertionResultData(
                assertion_id=spec.assertion_id,
                source=spec.source,
                path=spec.path,
                operator=spec.operator,
                expected=expected,
                actual=None if actual is MISSING else actual,
                verdict=verdict,
                evidence_refs=tuple(envelope.evidence_refs),
                source_timestamp=(
                    envelope.source_timestamp.isoformat()
                    if envelope.source_timestamp is not None else None
                ),
                route=dict(envelope.route) if envelope.route else None,
                reason=reason,
            ))

        if saw_fail:
            verdict = TestVerdict.FAIL
            reason = None
        elif inconclusive_reason or saw_inconclusive:
            verdict = TestVerdict.INCONCLUSIVE
            reason = inconclusive_reason or "ASSERTION_EVIDENCE_INCOMPLETE"
        else:
            verdict = TestVerdict.PASS
            reason = None

        # A test may be deterministically failed even if cleanup later fails, but
        # cleanup failure can never leave an apparent PASS.
        if verdict == TestVerdict.PASS and not cleanup_verified:
            verdict = TestVerdict.INCONCLUSIVE
            reason = "CLEANUP_NOT_VERIFIED"
        return AssertionEvaluation(verdict, tuple(results), reason)
