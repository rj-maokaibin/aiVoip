"""PBX adapters used by controlled VOIP automation gates."""

from .base import SipRegistrationEvidence, SipRegistrationProbe, TemporaryExtensionProvider, TemporaryExtensionSpec
from .fusionpbx_local import FusionPbxLocalProvider, FusionPbxSourceContractError

__all__ = [
    "FusionPbxLocalProvider",
    "FusionPbxSourceContractError",
    "SipRegistrationEvidence",
    "SipRegistrationProbe",
    "TemporaryExtensionProvider",
    "TemporaryExtensionSpec",
]
