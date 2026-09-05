"""PBX read-side adapters for controlled VOIP automation."""

from app.automation.adapters.pbx.registration import (
    FusionPbxRegistrationProbe,
    FusionPbxRegistrationProbeError,
)

__all__ = [
    "FusionPbxRegistrationProbe",
    "FusionPbxRegistrationProbeError",
]
