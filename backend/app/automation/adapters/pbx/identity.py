from __future__ import annotations

from dataclasses import dataclass
import re
import string
from urllib.parse import quote


class PbxExtensionIdentityError(ValueError):
    pass


# RFC 3261: user = 1*( unreserved / escaped / user-unreserved )
# unreserved = alphanum / mark; mark = -_.!~*'()
# user-unreserved = & = + $ , ; ? /
RFC3261_DIRECT_USER_CHARS = frozenset(
    string.ascii_letters + string.digits + "-_.!~*'()&=+$,;?/"
)
_HEX = frozenset(string.hexdigits)

# Product capability is intentionally narrower than the test-system core.
PRODUCT_MANAGED_IDENTITY_RE = re.compile(
    r"^(?=.{1,64}$)(?=.*[0-9A-Za-z])[0-9A-Za-z._+-]+$"
)
_DIAL_ALIAS_RE = re.compile(r"^[0-9]{1,32}$")

@dataclass(frozen=True)
class SipUserIdentity:
    logical: str
    wire: str
    escaped: bool
    provider_mutation_safe: bool

    def safe_dict(self) -> dict[str, object]:
        return {
            "logical": self.logical,
            "wire": self.wire,
            "escaped": self.escaped,
            "provider_mutation_safe": self.provider_mutation_safe,
        }


def normalize_sip_user_wire_identity(value: str, *, max_length: int = 512) -> str:
    wire = str(value)
    if not wire or len(wire) > max_length:
        raise PbxExtensionIdentityError("SIP_USER_IDENTITY_LENGTH_INVALID")
    i = 0
    while i < len(wire):
        char = wire[i]
        if char in RFC3261_DIRECT_USER_CHARS:
            i += 1
            continue
        if char == "%" and i + 2 < len(wire) and wire[i + 1] in _HEX and wire[i + 2] in _HEX:
            i += 3
            continue
        raise PbxExtensionIdentityError("SIP_USER_IDENTITY_WIRE_INVALID")
    return wire

def encode_sip_user_identity(logical: str, *, max_wire_length: int = 512) -> SipUserIdentity:
    value = str(logical)
    if not value:
        raise PbxExtensionIdentityError("SIP_USER_IDENTITY_REQUIRED")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise PbxExtensionIdentityError("SIP_USER_IDENTITY_CONTROL_CHAR_FORBIDDEN")
    wire = quote(value, safe="".join(sorted(RFC3261_DIRECT_USER_CHARS)), encoding="utf-8", errors="strict")
    normalize_sip_user_wire_identity(wire, max_length=max_wire_length)
    return SipUserIdentity(
        logical=value,
        wire=wire,
        escaped=(wire != value),
        provider_mutation_safe=is_testlab_provider_mutation_identity(value),
    )


def normalize_product_managed_identity(value: str) -> str:
    identity = str(value)
    if identity != identity.strip() or not PRODUCT_MANAGED_IDENTITY_RE.fullmatch(identity):
        raise PbxExtensionIdentityError("PBX_PRODUCT_IDENTITY_INVALID")
    return identity


def normalize_automation_identity(value: str) -> str:
    """Backward-compatible product-profile validator, not the test-system core."""
    try:
        return normalize_product_managed_identity(value)
    except PbxExtensionIdentityError as exc:
        raise PbxExtensionIdentityError("PBX_AUTOMATION_IDENTITY_INVALID") from exc


def is_automation_identity(value: str) -> bool:
    try:
        normalize_automation_identity(value)
    except PbxExtensionIdentityError:
        return False
    return True

def normalize_testlab_provider_mutation_identity(value: str, *, max_length: int = 128) -> str:
    """FusionPBX Test Lab safe subset of RFC3261 direct user characters.

    '/' is protocol-legal but is rejected for mutation because the current
    FusionPBX file-cache backend derives filesystem paths from directory keys.
    Escaped identities remain Core/SIP codec capabilities until a provider
    proves decoded-vs-wire storage semantics safely.
    """
    identity = str(value)
    if not identity or len(identity) > max_length:
        raise PbxExtensionIdentityError("PBX_PROVIDER_IDENTITY_LENGTH_INVALID")
    if any(ch not in RFC3261_DIRECT_USER_CHARS for ch in identity):
        raise PbxExtensionIdentityError("PBX_PROVIDER_IDENTITY_NOT_DIRECT_RFC3261")
    if "/" in identity:
        raise PbxExtensionIdentityError("PBX_PROVIDER_IDENTITY_PATH_RISK")
    if "$" in identity:
        raise PbxExtensionIdentityError("PBX_PROVIDER_IDENTITY_XML_SANITIZE_LOSS")
    return identity


def is_testlab_provider_mutation_identity(value: str) -> bool:
    try:
        normalize_testlab_provider_mutation_identity(value)
    except PbxExtensionIdentityError:
        return False
    return True


def normalize_dial_alias(value: str) -> str:
    alias = str(value)
    if alias != alias.strip() or not _DIAL_ALIAS_RE.fullmatch(alias):
        raise PbxExtensionIdentityError("PBX_DIAL_ALIAS_INVALID")
    return alias

def normalize_read_only_identity(value: str, *, max_length: int = 512) -> str:
    """Broad non-mutating identity query contract.

    Read-only discovery may need to observe historical/provider identities that
    are outside both the product profile and current mutation-safe subset.
    """
    identity = str(value)
    if not identity or len(identity) > max_length:
        raise PbxExtensionIdentityError("PBX_READ_IDENTITY_LENGTH_INVALID")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in identity):
        raise PbxExtensionIdentityError("PBX_READ_IDENTITY_CONTROL_CHAR_FORBIDDEN")
    return identity
