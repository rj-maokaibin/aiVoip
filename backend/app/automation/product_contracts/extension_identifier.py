from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


class ExtensionContractError(ValueError):
    pass


@dataclass(frozen=True)
class ExtensionValidation:
    accepted: bool
    reason: str | None = None


@dataclass(frozen=True)
class ExtensionIdentifierContract:
    contract_id: str
    letters_upper: bool
    letters_lower: bool
    digits: bool
    allowed_specials: str
    dot_allow_leading: bool
    dot_allow_trailing: bool
    dot_allow_consecutive: bool
    plus_anywhere: bool
    case_sensitive: bool
    min_length: int | None
    max_length: int | None
    pure_alpha_allowed: bool
    pure_special_char_allowed: bool
    whitespace_allowed: bool
    disname_same_rule: bool
    auth_id_same_rule: bool
    number_auth_id_must_equal: bool
    capability_missing_fallback: str

    def validate(self, value: str) -> ExtensionValidation:
        if not isinstance(value, str) or value == "":
            return ExtensionValidation(False, "EMPTY_IDENTIFIER")
        if not self.whitespace_allowed and any(ch.isspace() for ch in value):
            return ExtensionValidation(False, "WHITESPACE_FORBIDDEN")
        if self.min_length is not None and len(value) < self.min_length:
            return ExtensionValidation(False, "IDENTIFIER_TOO_SHORT")
        if self.max_length is not None and len(value) > self.max_length:
            return ExtensionValidation(False, "IDENTIFIER_TOO_LONG")

        for ch in value:
            if "0" <= ch <= "9" and self.digits:
                continue
            if "A" <= ch <= "Z" and self.letters_upper:
                continue
            if "a" <= ch <= "z" and self.letters_lower:
                continue
            if ch in self.allowed_specials:
                continue
            return ExtensionValidation(False, f"CHARACTER_UNSUPPORTED:{ch}")

        if not self.pure_alpha_allowed and value.isascii() and value.isalpha():
            return ExtensionValidation(False, "PURE_ALPHA_FORBIDDEN")
        if not self.pure_special_char_allowed and all(ch in self.allowed_specials for ch in value):
            return ExtensionValidation(False, "PURE_SPECIAL_FORBIDDEN")
        if "." in value:
            if value.startswith(".") and not self.dot_allow_leading:
                return ExtensionValidation(False, "LEADING_DOT_FORBIDDEN")
            if value.endswith(".") and not self.dot_allow_trailing:
                return ExtensionValidation(False, "TRAILING_DOT_FORBIDDEN")
            if ".." in value and not self.dot_allow_consecutive:
                return ExtensionValidation(False, "CONSECUTIVE_DOT_FORBIDDEN")
        if "+" in value and not self.plus_anywhere:
            return ExtensionValidation(False, "PLUS_POSITION_FORBIDDEN")
        return ExtensionValidation(True)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ExtensionIdentifierContract":
        allowed_top = {"id", "capability_missing_fallback", "extension_identifier", "voip_identity_fields", "acceptance"}
        unknown = set(raw) - allowed_top
        if unknown:
            raise ExtensionContractError(f"EXTENSION_CONTRACT_UNKNOWN_FIELDS:{sorted(unknown)}")
        identifier = raw.get("extension_identifier")
        fields = raw.get("voip_identity_fields")
        if not isinstance(identifier, Mapping) or not isinstance(fields, Mapping):
            raise ExtensionContractError("EXTENSION_CONTRACT_REQUIRED_FIELDS_MISSING")
        dot = identifier.get("dot")
        plus = identifier.get("plus")
        if not isinstance(dot, Mapping) or not isinstance(plus, Mapping):
            raise ExtensionContractError("EXTENSION_SPECIAL_RULES_MISSING")
        specials = ""
        if bool(dot.get("supported")):
            specials += "."
        if bool(plus.get("supported")):
            specials += "+"
        return cls(
            contract_id=str(raw.get("id") or ""),
            letters_upper=bool(identifier.get("upper_A_Z")),
            letters_lower=bool(identifier.get("lower_a_z")),
            digits=bool(identifier.get("digits_0_9")),
            allowed_specials=specials,
            dot_allow_leading=bool(dot.get("allow_leading")),
            dot_allow_trailing=bool(dot.get("allow_trailing")),
            dot_allow_consecutive=bool(dot.get("allow_consecutive")),
            plus_anywhere=str(plus.get("position") or "") == "anywhere",
            case_sensitive=bool(identifier.get("case_sensitive")),
            min_length=int(identifier["min_length"]) if identifier.get("min_length") is not None else None,
            max_length=int(identifier["max_length"]) if identifier.get("max_length") is not None else None,
            pure_alpha_allowed=bool(identifier.get("pure_alpha_allowed")),
            pure_special_char_allowed=bool(identifier.get("pure_special_char_allowed")),
            whitespace_allowed=bool(identifier.get("whitespace_allowed")),
            disname_same_rule=bool(fields.get("disName_same_rule")),
            auth_id_same_rule=bool(fields.get("authId_same_rule")),
            number_auth_id_must_equal=bool(fields.get("number_authId_must_equal")),
            capability_missing_fallback=str(raw.get("capability_missing_fallback") or ""),
        )


LEGACY_DIGITS_ONLY_CONTRACT = ExtensionIdentifierContract(
    contract_id="legacy-no-capability-digits-only",
    letters_upper=False,
    letters_lower=False,
    digits=True,
    allowed_specials="",
    dot_allow_leading=False,
    dot_allow_trailing=False,
    dot_allow_consecutive=False,
    plus_anywhere=False,
    case_sensitive=True,
    min_length=None,
    max_length=None,
    pure_alpha_allowed=False,
    pure_special_char_allowed=False,
    whitespace_allowed=False,
    disname_same_rule=False,
    auth_id_same_rule=False,
    number_auth_id_must_equal=False,
    capability_missing_fallback="digits_only",
)


def load_extension_identifier_contract(path: str | Path) -> tuple[ExtensionIdentifierContract, Mapping[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, Mapping):
        raise ExtensionContractError("EXTENSION_CONTRACT_ROOT_INVALID")
    return ExtensionIdentifierContract.from_mapping(raw), raw


def effective_contract(current: ExtensionIdentifierContract, *, capability_present: bool) -> ExtensionIdentifierContract:
    if capability_present:
        return current
    if current.capability_missing_fallback != "digits_only":
        raise ExtensionContractError("LEGACY_FALLBACK_UNSUPPORTED")
    return LEGACY_DIGITS_ONLY_CONTRACT
