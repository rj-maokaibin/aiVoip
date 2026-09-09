from __future__ import annotations

import re


class PbxExtensionIdentityError(ValueError):
    pass


# Product automation contract: ASCII letters/digits plus only '.' and '+'.
# At least one alphanumeric character is required; '@', whitespace, '/', ':',
# '_', '-' and other punctuation are intentionally outside V1 managed scope.
_AUTOMATION_IDENTITY_RE = re.compile(
    r"^(?=.{1,64}$)(?=.*[0-9A-Za-z])[0-9A-Za-z.+]+$"
)


def normalize_automation_identity(value: str) -> str:
    identity = str(value)
    if identity != identity.strip() or not _AUTOMATION_IDENTITY_RE.fullmatch(identity):
        raise PbxExtensionIdentityError("PBX_AUTOMATION_IDENTITY_INVALID")
    return identity


def is_automation_identity(value: str) -> bool:
    try:
        normalize_automation_identity(value)
    except PbxExtensionIdentityError:
        return False
    return True
