from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.capture_v2.control import production_cutover_guarded_base as _base


# Older production.env files predate these V2 switches.  Missing values have the
# same effective semantics as the application/runtime defaults and therefore do
# not represent a safety downgrade.  Explicit values are never overwritten.
_EFFECTIVE_PRESTATE_DEFAULTS = {
    "CAPTURE_ENGINE_VERSION": "V1",
    "CAPTURE_V2_PRODUCTION_ENABLED": "false",
    "CAPTURE_V2_ACTIVATION_REHEARSAL": "false",
}
_DEFAULTED_MARKER = "__CAPTURE_V2_EFFECTIVE_DEFAULTED_KEYS"
_ORIGINAL_READ_SAFE_ENV = _base._read_safe_env


def _read_safe_env_with_effective_defaults(repo_root: Path) -> tuple[int, dict[str, str], str]:
    rc, values, error = _ORIGINAL_READ_SAFE_ENV(repo_root)
    if rc != 0:
        return rc, values, error
    normalized = dict(values)
    defaulted: list[str] = []
    for key, default in _EFFECTIVE_PRESTATE_DEFAULTS.items():
        raw = normalized.get(key)
        if raw is None or str(raw).strip() == "":
            normalized[key] = default
            defaulted.append(key)
    if defaulted:
        normalized[_DEFAULTED_MARKER] = ",".join(sorted(defaulted))
    return rc, normalized, error


def run(*, repo_root: Path, authorization_path: Path) -> tuple[int, dict[str, Any]]:
    original = _base._read_safe_env
    _base._read_safe_env = _read_safe_env_with_effective_defaults
    try:
        rc, payload = _base.run(repo_root=repo_root, authorization_path=authorization_path)
    finally:
        _base._read_safe_env = original

    pre_env = payload.get("pre_env")
    if isinstance(pre_env, dict):
        marker = str(pre_env.pop(_DEFAULTED_MARKER, "") or "")
        if marker:
            payload["pre_env_defaulted_keys"] = [x for x in marker.split(",") if x]
            payload["pre_env_defaults_source"] = "APPLICATION_RUNTIME_DEFAULTS"
    return rc, payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Guarded Capture V2 production cutover with legacy-env default normalization"
    )
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    args = parser.parse_args(argv)
    rc, payload = run(repo_root=args.repo_root, authorization_path=args.authorization)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
