import json
import os
from pathlib import Path

from app.analyzers.packet.tshark import TSharkAdapter


def test_tshark_adapter_streams_ek_records(tmp_path:Path):
    fake=tmp_path/'fake-tshark'
    packet={
        'layers':{
            'frame':{'frame_frame_number':'7','frame_frame_time_epoch':'123.5'},
            'ip':{'ip_ip_src':'10.0.0.1','ip_ip_dst':'10.0.0.2'},
            'udp':{'udp_udp_srcport':'5060','udp_udp_dstport':'5060'},
            'sip':{'sip_sip_method':'REGISTER','sip_sip_call_id':'reg-1'}
        }
    }
    script=f'''#!/bin/sh
if [ "$1" = "-v" ]; then
  echo "TShark (Wireshark) 4.fake"
  exit 0
fi
echo '{{"index":{{}}}}'
echo '{json.dumps(packet)}'
'''
    fake.write_text(script)
    fake.chmod(0o755)
    pcap=tmp_path/'x.pcap'; pcap.write_bytes(b'fake')
    adapter=TSharkAdapter(str(fake), timeout_seconds=2)
    packets=list(adapter.iter_packets(pcap))
    assert adapter.version() == 'TShark (Wireshark) 4.fake'
    assert len(packets)==1
    assert packets[0].sip.method=='REGISTER'
    assert packets[0].frame_number==7
