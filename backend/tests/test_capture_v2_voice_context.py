import asyncio

from app.capture_v2.voice_context import VoiceContextResolverV2


class FakeReader:
    async def run(self, command):
        if command == "dev_config get -m voipServInfo":
            return '{"data":[{"svrName":"192.168.3.200","svrPort":"5060"}]}'
        if command == "dev_config get -m voice_vlan":
            return '{"enable":1,"vlanid":400}'
        if command == "ip -o link show":
            return '17: br-lan_400: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP\n'
        raise AssertionError(command)


def test_voice_context_reuses_validated_common_resolvers():
    ctx = asyncio.run(VoiceContextResolverV2(FakeReader()).resolve())
    assert ctx.gateway_ip == "192.168.3.200"
    assert ctx.voice_vlan_id == "400"
    assert ctx.interface == "br-lan_400"
