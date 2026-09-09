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
    "PbxResourceManager",
    "FreeSwitchRuntimeReadProbe",
    "normalize_automation_identity",
    "PbxExtensionIdentityError",
    "FreeSwitchRegistrationObserver",
    "FusionPbxMutationProvider",
    "FreeSwitchEventSocketClient",
    "FreeSwitchRuntimeEvent",
    "FreeSwitchEslError",
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
    if name == "FusionPbxMutationProvider":
        from app.automation.adapters.pbx.fusionpbx_mutation import FusionPbxMutationProvider
        return FusionPbxMutationProvider
    if name in {"FreeSwitchEventSocketClient", "FreeSwitchRuntimeEvent", "FreeSwitchEslError"}:
        from app.automation.adapters.pbx.esl import (
            FreeSwitchEventSocketClient, FreeSwitchRuntimeEvent, FreeSwitchEslError
        )
        return {
            "FreeSwitchEventSocketClient": FreeSwitchEventSocketClient,
            "FreeSwitchRuntimeEvent": FreeSwitchRuntimeEvent,
            "FreeSwitchEslError": FreeSwitchEslError,
        }[name]
    if name == "FreeSwitchRuntimeReadProbe":
        from app.automation.adapters.pbx.runtime_read import FreeSwitchRuntimeReadProbe
        return FreeSwitchRuntimeReadProbe
    if name == "FreeSwitchRegistrationObserver":
        from app.automation.adapters.pbx.registration_esl import FreeSwitchRegistrationObserver
        return FreeSwitchRegistrationObserver
    if name in {"PbxExtensionIdentityError", "normalize_automation_identity"}:
        from app.automation.adapters.pbx.identity import PbxExtensionIdentityError, normalize_automation_identity
        return {"PbxExtensionIdentityError": PbxExtensionIdentityError, "normalize_automation_identity": normalize_automation_identity}[name]
    if name == "PbxResourceManager":
        from app.automation.adapters.pbx.resource_manager import PbxResourceManager
        return PbxResourceManager
    raise AttributeError(name)
