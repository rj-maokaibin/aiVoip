from __future__ import annotations

from dataclasses import dataclass

from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.transport.readonly import ReadOnlyDeviceTransport
from app.platforms.resolvers import (
    PlatformResolverError,
    resolve_voip_service_gateway_v1,
    resolve_voice_interface_v1,
    resolve_voice_vlan_id_v1,
)


@dataclass(frozen=True)
class VoiceContextV2:
    gateway_ip: str
    voice_vlan_id: str
    interface: str


class VoiceContextResolverV2:
    """Resolve immutable session voice context through read-only DUT commands.

    The parsers are the already-validated platform resolvers used by V1, while
    command ownership lives in Capture V2.  No RealReproductionPlatform object is
    constructed, so resolving context cannot accidentally start a V1 producer.
    """

    def __init__(self, reader: ReadOnlyDeviceTransport):
        self.reader = reader

    async def resolve(self) -> VoiceContextV2:
        try:
            service_raw = await self.reader.run("dev_config get -m voipServInfo")
            vlan_raw = await self.reader.run("dev_config get -m voice_vlan")
            links_raw = await self.reader.run("ip -o link show")
            gateway = resolve_voip_service_gateway_v1(service_raw)
            vlan_id = resolve_voice_vlan_id_v1(vlan_raw)
            interface = resolve_voice_interface_v1(links_raw, voice_vlan_id=vlan_id)
        except PlatformResolverError as exc:
            raise CaptureV2Error("VOICE_CONTEXT_INVALID", details={"reason": str(exc)}) from exc
        return VoiceContextV2(
            gateway_ip=gateway,
            voice_vlan_id=vlan_id,
            interface=interface,
        )
