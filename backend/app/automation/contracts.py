from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Mapping

from app.infrastructure.action_route import ActionEntry, ActionPurpose


class DSLValidationError(ValueError):
    pass


class TestContractStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PROVISIONAL = "PROVISIONAL"
    RESERVED = "RESERVED"
    DISABLED = "DISABLED"


class TestVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    INCONCLUSIVE = "INCONCLUSIVE"


ALLOWED_ASSERTION_SOURCES = frozenset({
    "entry", "http", "config_framework", "sip", "rtp",
    "pcm", "logs", "pbx", "system",
})
ALLOWED_ASSERTION_OPERATORS = frozenset({
    "eq", "ne", "contains", "regex", "exists", "not_exists",
    "gt", "lt", "range", "count", "duration",
})
_FORBIDDEN_KEYS = frozenset({
    "command", "raw_command", "raw_shell", "shell", "ssh_command",
    "url", "endpoint", "http_url", "topic", "mqtt_topic",
    "fs_cli", "fs_cli_command", "tcpdump_args", "tcpdump_argument",
})
_SECRET_KEYS = frozenset({"password", "passwd", "secret", "token", "cookie", "csrf", "authorization", "sid"})
_ALLOWED_TOP_LEVEL = frozenset({
    "id", "version", "name", "suite_id", "entry", "contract_status",
    "environment", "parameters", "snapshot", "steps", "assertions", "cleanup",
})
_SEMANTIC_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
_TIMEOUT = re.compile(r"^(?P<num>[0-9]+(?:\.[0-9]+)?)(?P<unit>ms|s|m)?$")


@dataclass(frozen=True)
class WaitForSpec:
    event: str
    timeout_seconds: float


@dataclass(frozen=True)
class ActionStepSpec:
    action: str
    purpose: ActionPurpose
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AssertionSpec:
    assertion_id: str
    source: str
    path: str
    operator: str
    expected: Any = None


@dataclass(frozen=True)
class CleanupSpec:
    strategy: str = "restore_snapshot"
    verify: bool = True


@dataclass(frozen=True)
class TestCaseSpec:
    case_id: str
    version: int
    name: str
    suite_id: str
    entry: ActionEntry
    contract_status: TestContractStatus
    environment_profile: str
    parameters: dict[str, Any]
    snapshot: tuple[str, ...]
    steps: tuple[ActionStepSpec | WaitForSpec, ...]
    assertions: tuple[AssertionSpec, ...]
    cleanup: CleanupSpec

    @property
    def executable(self) -> bool:
        return self.contract_status not in {
            TestContractStatus.RESERVED,
            TestContractStatus.DISABLED,
        }


def parse_timeout(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value <= 0:
            raise DSLValidationError("TIMEOUT_MUST_BE_POSITIVE")
        return float(value)
    if not isinstance(value, str):
        raise DSLValidationError("INVALID_TIMEOUT")
    match = _TIMEOUT.fullmatch(value.strip())
    if not match:
        raise DSLValidationError(f"INVALID_TIMEOUT:{value}")
    number = float(match.group("num"))
    unit = match.group("unit") or "s"
    multiplier = {"ms": 0.001, "s": 1.0, "m": 60.0}[unit]
    seconds = number * multiplier
    if seconds <= 0:
        raise DSLValidationError("TIMEOUT_MUST_BE_POSITIVE")
    return seconds


def _secret_plaintext_key(key: str) -> bool:
    lowered = key.lower()
    if lowered.endswith("_ref"):
        return False
    if lowered in _SECRET_KEYS:
        return True
    return lowered.endswith((
        "_password", "_passwd", "_secret", "_token", "_cookie",
        "_csrf", "_authorization", "_sid",
    ))


def _reject_forbidden(value: Any, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if lowered in _FORBIDDEN_KEYS:
                raise DSLValidationError(f"FORBIDDEN_CASE_FIELD:{path}.{key}")
            if _secret_plaintext_key(lowered):
                if child not in (None, ""):
                    raise DSLValidationError(f"PLAINTEXT_SECRET_FORBIDDEN:{path}.{key}")
            _reject_forbidden(child, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_forbidden(child, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered.startswith(("http://", "https://", "ssh://", "mqtt://")):
            raise DSLValidationError(f"RAW_TRANSPORT_ADDRESS_FORBIDDEN:{path}")
        if lowered.startswith("fs_cli ") or lowered == "fs_cli":
            raise DSLValidationError(f"RAW_FS_CLI_FORBIDDEN:{path}")
        if lowered.startswith("tcpdump ") or lowered == "tcpdump":
            raise DSLValidationError(f"RAW_TCPDUMP_FORBIDDEN:{path}")


def _require_mapping(value: Any, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DSLValidationError(code)
    return value


def _parse_action_step(raw: Mapping[str, Any]) -> ActionStepSpec:
    allowed = {"action", "purpose", "args"}
    unknown = set(raw) - allowed
    if unknown:
        raise DSLValidationError(f"UNKNOWN_ACTION_STEP_FIELDS:{sorted(unknown)}")
    action = raw.get("action")
    if not isinstance(action, str) or not _SEMANTIC_ID.fullmatch(action):
        raise DSLValidationError("INVALID_SEMANTIC_ACTION")
    if action.startswith(("ssh.", "http.", "mqtt.")) or action in {"shell.exec", "raw.exec"}:
        raise DSLValidationError(f"RAW_TRANSPORT_ACTION_FORBIDDEN:{action}")
    try:
        purpose = ActionPurpose(str(raw.get("purpose")))
    except Exception as exc:
        raise DSLValidationError(f"INVALID_ACTION_PURPOSE:{raw.get('purpose')}") from exc
    args = raw.get("args", {})
    if not isinstance(args, Mapping):
        raise DSLValidationError(f"INVALID_ACTION_ARGS:{action}")
    _reject_forbidden(args, path=f"$.steps.{action}.args")
    return ActionStepSpec(action=action, purpose=purpose, args=dict(args))


def _parse_wait_step(raw: Mapping[str, Any]) -> WaitForSpec:
    if set(raw) != {"wait_for"}:
        raise DSLValidationError("WAIT_STEP_MUST_ONLY_CONTAIN_WAIT_FOR")
    wait = _require_mapping(raw["wait_for"], "INVALID_WAIT_FOR")
    unknown = set(wait) - {"event", "timeout"}
    if unknown:
        raise DSLValidationError(f"UNKNOWN_WAIT_FIELDS:{sorted(unknown)}")
    event = wait.get("event")
    if not isinstance(event, str) or not _SEMANTIC_ID.fullmatch(event):
        raise DSLValidationError("INVALID_EVENT_NAME")
    if "timeout" not in wait:
        raise DSLValidationError("WAIT_TIMEOUT_REQUIRED")
    return WaitForSpec(event=event, timeout_seconds=parse_timeout(wait["timeout"]))


def _parse_assertion(raw: Mapping[str, Any], index: int) -> AssertionSpec:
    allowed = {"id", "source", "path", "op", "value"}
    unknown = set(raw) - allowed
    if unknown:
        raise DSLValidationError(f"UNKNOWN_ASSERTION_FIELDS:{sorted(unknown)}")
    source = raw.get("source")
    if source not in ALLOWED_ASSERTION_SOURCES:
        raise DSLValidationError(f"INVALID_ASSERTION_SOURCE:{source}")
    operator = raw.get("op")
    if operator not in ALLOWED_ASSERTION_OPERATORS:
        raise DSLValidationError(f"INVALID_ASSERTION_OPERATOR:{operator}")
    path = raw.get("path", "")
    if not isinstance(path, str):
        raise DSLValidationError("INVALID_ASSERTION_PATH")
    assertion_id = raw.get("id") or f"a{index:03d}"
    if not isinstance(assertion_id, str) or not _SEMANTIC_ID.fullmatch(assertion_id):
        raise DSLValidationError(f"INVALID_ASSERTION_ID:{assertion_id}")
    expected = raw.get("value")
    _reject_forbidden(expected, path=f"$.assertions[{index}].value")
    return AssertionSpec(
        assertion_id=assertion_id,
        source=str(source),
        path=path,
        operator=str(operator),
        expected=expected,
    )


def parse_test_case(raw: Mapping[str, Any]) -> TestCaseSpec:
    if not isinstance(raw, Mapping):
        raise DSLValidationError("TEST_CASE_MUST_BE_MAPPING")
    unknown = set(raw) - _ALLOWED_TOP_LEVEL
    if unknown:
        raise DSLValidationError(f"UNKNOWN_TEST_CASE_FIELDS:{sorted(unknown)}")
    _reject_forbidden(raw)

    case_id = raw.get("id")
    if not isinstance(case_id, str) or not _SEMANTIC_ID.fullmatch(case_id):
        raise DSLValidationError("INVALID_TEST_CASE_ID")
    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        raise DSLValidationError("INVALID_TEST_CASE_VERSION")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise DSLValidationError("INVALID_TEST_CASE_NAME")
    suite_id = raw.get("suite_id", "default")
    if not isinstance(suite_id, str) or not _SEMANTIC_ID.fullmatch(suite_id):
        raise DSLValidationError("INVALID_SUITE_ID")
    try:
        entry = ActionEntry(str(raw.get("entry")))
    except Exception as exc:
        raise DSLValidationError(f"INVALID_TEST_ENTRY:{raw.get('entry')}") from exc
    try:
        contract_status = TestContractStatus(str(raw.get("contract_status", "ACTIVE")).upper())
    except Exception as exc:
        raise DSLValidationError(f"INVALID_TEST_CONTRACT_STATUS:{raw.get('contract_status')}") from exc

    environment = _require_mapping(raw.get("environment"), "ENVIRONMENT_PROFILE_REQUIRED")
    if set(environment) != {"profile"}:
        raise DSLValidationError("ENVIRONMENT_MUST_REFERENCE_PROFILE_ONLY")
    environment_profile = environment.get("profile")
    if not isinstance(environment_profile, str) or not _SEMANTIC_ID.fullmatch(environment_profile):
        raise DSLValidationError("INVALID_ENVIRONMENT_PROFILE")

    parameters = raw.get("parameters", {})
    if not isinstance(parameters, Mapping):
        raise DSLValidationError("INVALID_PARAMETERS")
    snapshot_raw = raw.get("snapshot", [])
    if not isinstance(snapshot_raw, list) or not all(isinstance(item, str) and _SEMANTIC_ID.fullmatch(item) for item in snapshot_raw):
        raise DSLValidationError("INVALID_SNAPSHOT_LIST")

    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise DSLValidationError("TEST_STEPS_REQUIRED")
    steps: list[ActionStepSpec | WaitForSpec] = []
    for index, step_raw in enumerate(steps_raw, start=1):
        step = _require_mapping(step_raw, f"INVALID_STEP:{index}")
        if "action" in step:
            if "wait_for" in step:
                raise DSLValidationError(f"STEP_CANNOT_MIX_ACTION_AND_WAIT:{index}")
            steps.append(_parse_action_step(step))
        elif "wait_for" in step:
            steps.append(_parse_wait_step(step))
        else:
            raise DSLValidationError(f"STEP_TYPE_REQUIRED:{index}")

    assertions_raw = raw.get("assertions")
    if not isinstance(assertions_raw, list) or not assertions_raw:
        raise DSLValidationError("ASSERTIONS_REQUIRED")
    assertions = tuple(
        _parse_assertion(_require_mapping(item, f"INVALID_ASSERTION:{index}"), index)
        for index, item in enumerate(assertions_raw, start=1)
    )

    cleanup_raw = _require_mapping(raw.get("cleanup"), "CLEANUP_REQUIRED")
    unknown_cleanup = set(cleanup_raw) - {"strategy", "verify"}
    if unknown_cleanup:
        raise DSLValidationError(f"UNKNOWN_CLEANUP_FIELDS:{sorted(unknown_cleanup)}")
    strategy = cleanup_raw.get("strategy", "restore_snapshot")
    if strategy not in {"restore_snapshot", "none"}:
        raise DSLValidationError(f"INVALID_CLEANUP_STRATEGY:{strategy}")
    verify = cleanup_raw.get("verify", True)
    if not isinstance(verify, bool):
        raise DSLValidationError("INVALID_CLEANUP_VERIFY")
    if strategy != "none" and not verify:
        raise DSLValidationError("MUTATING_CLEANUP_MUST_VERIFY")

    return TestCaseSpec(
        case_id=case_id,
        version=version,
        name=name,
        suite_id=suite_id,
        entry=entry,
        contract_status=contract_status,
        environment_profile=environment_profile,
        parameters=dict(parameters),
        snapshot=tuple(snapshot_raw),
        steps=tuple(steps),
        assertions=assertions,
        cleanup=CleanupSpec(strategy=strategy, verify=verify),
    )
