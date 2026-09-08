"""PBX adapters and infrastructure gates.

Exports are lazy so standalone read-only health tooling does not pull the full
WEB/Golden/SQLAlchemy runtime dependency graph.
"""

__all__ = [
    "FusionPbxConfigProvider",
    "FusionPbxConfigProviderError",
    "FusionPbxLabProfile",
    "FusionPbxRegistrationProbe",
    "FusionPbxRegistrationProbeError",
    "FusionPbxSourceFence",
    "FusionPbxSourceFenceError",
    "PbxHealthGate",
    "PbxHealthResult",
    "load_fusionpbx_profile",
]


def __getattr__(name: str):
    if name in {"FusionPbxConfigProvider", "FusionPbxConfigProviderError"}:
        from app.automation.adapters.pbx.fusionpbx_config import (
            FusionPbxConfigProvider,
            FusionPbxConfigProviderError,
        )
        return {"FusionPbxConfigProvider": FusionPbxConfigProvider, "FusionPbxConfigProviderError": FusionPbxConfigProviderError}[name]
    if name in {"FusionPbxLabProfile", "load_fusionpbx_profile"}:
        from app.automation.adapters.pbx.profile import FusionPbxLabProfile, load_fusionpbx_profile
        return {"FusionPbxLabProfile": FusionPbxLabProfile, "load_fusionpbx_profile": load_fusionpbx_profile}[name]
    if name in {"FusionPbxRegistrationProbe", "FusionPbxRegistrationProbeError"}:
        from app.automation.adapters.pbx.registration import FusionPbxRegistrationProbe, FusionPbxRegistrationProbeError
        return {"FusionPbxRegistrationProbe": FusionPbxRegistrationProbe, "FusionPbxRegistrationProbeError": FusionPbxRegistrationProbeError}[name]
    if name in {"FusionPbxSourceFence", "FusionPbxSourceFenceError"}:
        from app.automation.adapters.pbx.source_fence import FusionPbxSourceFence, FusionPbxSourceFenceError
        return {"FusionPbxSourceFence": FusionPbxSourceFence, "FusionPbxSourceFenceError": FusionPbxSourceFenceError}[name]
    if name in {"PbxHealthGate", "PbxHealthResult"}:
        from app.automation.adapters.pbx.health import PbxHealthGate, PbxHealthResult
        return {"PbxHealthGate": PbxHealthGate, "PbxHealthResult": PbxHealthResult}[name]
    raise AttributeError(name)
