from __future__ import annotations

import sys
from pathlib import Path

from .policy_base import ControlPolicy as _BaseControlPolicy
from .policy_base import ControlPolicyError, PreparedCommand
from .schema import ControlActionType, RemoteAction


_CANONICAL_PRODUCTION_AUTHORIZATION = Path(
    "validation/capture_v2/PRODUCTION_CUTOVER_AUTHORIZATION_RC69.json"
)


class ControlPolicy(_BaseControlPolicy):
    """Fail-closed extension for explicit production control actions.

    All legacy actions are delegated byte-for-byte to ``policy_base``.  The
    elevated production surface is intentionally limited to the two existing,
    audited guarded Python modules below.  Remote actions cannot override the
    production env path, authorization path, module, interpreter, or command.
    """

    def prepare(self, action: RemoteAction) -> PreparedCommand | None:
        if action.action_type not in {
            ControlActionType.PRODUCTION_DEPLOYMENT_PREFLIGHT,
            ControlActionType.PRODUCTION_CUTOVER,
        }:
            return super().prepare(action)

        p = action.parameters
        try:
            timeout = float(p.get("timeout_seconds", 900.0))
        except (TypeError, ValueError) as exc:
            raise ControlPolicyError("TIMEOUT_OUT_OF_RANGE") from exc
        if timeout <= 0 or timeout > 7200:
            raise ControlPolicyError("TIMEOUT_OUT_OF_RANGE")

        unknown = set(p) - {"timeout_seconds"}
        if unknown:
            prefix = (
                "PRODUCTION_DEPLOYMENT_PREFLIGHT"
                if action.action_type == ControlActionType.PRODUCTION_DEPLOYMENT_PREFLIGHT
                else "PRODUCTION_CUTOVER"
            )
            raise ControlPolicyError(
                f"{prefix}_PARAMETERS_NOT_ALLOWED:" + ",".join(sorted(unknown))
            )

        sudo = Path("/usr/bin/sudo")
        if not sudo.is_file():
            raise ControlPolicyError("PRODUCTION_CONTROL_SUDO_NOT_AVAILABLE")

        authorization = (self.repo_root / _CANONICAL_PRODUCTION_AUTHORIZATION).resolve()
        env = __import__("os").environ.copy()
        env.setdefault("CAPTURE_ENGINE_VERSION", "V1")
        env.setdefault("CAPTURE_V2_PRODUCTION_ENABLED", "false")
        env.setdefault("PYTHONPATH", ".")

        if action.action_type == ControlActionType.PRODUCTION_DEPLOYMENT_PREFLIGHT:
            module = "app.capture_v2.control.production_deployment_preflight_guarded"
            effective_timeout = min(timeout, 300.0)
        else:
            module = "app.capture_v2.control.production_cutover_guarded"
            effective_timeout = timeout

        argv = [
            str(sudo),
            "-n",
            sys.executable,
            "-m",
            module,
            "--repo-root",
            str(self.repo_root),
            "--authorization",
            str(authorization),
        ]
        return PreparedCommand(
            argv=argv,
            cwd=self.backend_root,
            timeout_seconds=effective_timeout,
            env=env,
            result_kind="gate-cli",
        )


__all__ = ["ControlPolicy", "ControlPolicyError", "PreparedCommand"]
