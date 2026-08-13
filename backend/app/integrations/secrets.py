from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass


class SecretResolutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class SecretRef:
    value: str = ""
    file: str = ""
    env: str = ""

    def configured(self) -> bool:
        return bool(self.value or self.file or self.env)


class SecretResolver:
    """Resolve secrets without logging or persisting their values.

    Resolution order is explicit file -> env-name -> direct value. Production
    deployments should prefer mounted secret files or environment injection from
    the platform secret store. Direct values remain supported for dev/e2e only.
    """

    @staticmethod
    def resolve(ref: SecretRef, *, name: str, required: bool = False) -> str:
        if ref.file:
            path = Path(ref.file)
            try:
                value = path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise SecretResolutionError(f"{name}_SECRET_FILE_UNREADABLE") from exc
            if value:
                return value
        if ref.env:
            value = os.getenv(ref.env, "").strip()
            if value:
                return value
        value = str(ref.value or "").strip()
        if value:
            return value
        if required:
            raise SecretResolutionError(f"{name}_SECRET_NOT_CONFIGURED")
        return ""

    @staticmethod
    def is_non_default(value: str, defaults: set[str]) -> bool:
        normalized = str(value or "").strip()
        return bool(normalized and normalized not in defaults)
