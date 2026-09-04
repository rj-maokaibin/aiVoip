from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import yaml

from app.automation.contracts import TestCaseSpec, parse_test_case


class TestRegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class TestDefinition:
    case: TestCaseSpec
    checksum: str
    source_path: str

    @property
    def version(self) -> int:
        return self.case.version


class TestRegistry:
    """Strict, source-bound registry for declarative test cases."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._definitions: dict[str, TestDefinition] = {}
        self.reload()

    @staticmethod
    def checksum(raw: Mapping[str, Any]) -> str:
        canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def reload(self) -> None:
        definitions: dict[str, TestDefinition] = {}
        if not self.root.exists():
            self._definitions = {}
            return
        for path in sorted(self.root.rglob("*.yaml")):
            try:
                document = yaml.safe_load(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise TestRegistryError(f"INVALID_TEST_YAML:{path.name}:{type(exc).__name__}") from exc
            if document is None:
                continue
            if isinstance(document, Mapping) and "cases" in document:
                unknown = set(document) - {"cases"}
                if unknown:
                    raise TestRegistryError(f"UNKNOWN_TEST_DOCUMENT_FIELDS:{path.name}:{sorted(unknown)}")
                raw_cases = document["cases"]
            elif isinstance(document, Mapping):
                raw_cases = [document]
            else:
                raise TestRegistryError(f"TEST_DOCUMENT_MUST_BE_MAPPING:{path.name}")
            if not isinstance(raw_cases, list):
                raise TestRegistryError(f"TEST_CASES_MUST_BE_LIST:{path.name}")
            for raw in raw_cases:
                if not isinstance(raw, Mapping):
                    raise TestRegistryError(f"TEST_CASE_MUST_BE_MAPPING:{path.name}")
                try:
                    case = parse_test_case(raw)
                except Exception as exc:
                    raise TestRegistryError(f"INVALID_TEST_CASE:{path.name}:{exc}") from exc
                if case.case_id in definitions:
                    previous = definitions[case.case_id]
                    raise TestRegistryError(
                        f"DUPLICATE_TEST_ID:{case.case_id}:{previous.source_path}:{path}"
                    )
                definitions[case.case_id] = TestDefinition(
                    case=case,
                    checksum=self.checksum(raw),
                    source_path=str(path),
                )
        self._definitions = definitions

    def definition(self, case_id: str, *, require_executable: bool = True) -> TestDefinition:
        try:
            definition = self._definitions[case_id]
        except KeyError as exc:
            raise TestRegistryError(f"UNKNOWN_TEST_CASE:{case_id}") from exc
        if require_executable and not definition.case.executable:
            raise TestRegistryError(
                f"TEST_CASE_NOT_EXECUTABLE:{case_id}:{definition.case.contract_status.value}"
            )
        return definition

    def case(self, case_id: str, *, require_executable: bool = True) -> TestCaseSpec:
        return self.definition(case_id, require_executable=require_executable).case

    def all(self) -> tuple[TestDefinition, ...]:
        return tuple(self._definitions[key] for key in sorted(self._definitions))
