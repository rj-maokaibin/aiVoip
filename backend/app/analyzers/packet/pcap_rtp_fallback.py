from __future__ import annotations

from collections import defaultdict
import struct
from pathlib import Path

from app.analyzers.pcm.pcap_udp import iter_udp_datagrams
from .types import NormalizedPacket, RtpData


def read_rtp_packets_fallback(path: str | Path, *, exclude_ports: set[int] | None = None,
                              min_packets: int = 20) -> list[NormalizedPacket]:
    """Restricted classic-PCAP RTP fallback.

    It deliberately does not replace TShark. Candidate streams are accepted only
    after enough packets share the same 4-tuple/SSRC/PT and sequence numbers show
    RTP-like continuity. Intended for degraded media analysis when TShark fails.
    """
    exclude_ports = exclude_ports or set()
    candidates: dict[tuple, list[tuple]] = defaultdict(list)
    for d in iter_udp_datagrams(path):
        if d.src_port in exclude_ports or d.dst_port in exclude_ports:
            continue
        parsed = _parse_rtp(d.payload)
        if not parsed:
            continue
        seq, ts, ssrc, pt, marker, payload = parsed
        key=(d.src_ip,d.src_port,d.dst_ip,d.dst_port,ssrc,pt)
        candidates[key].append((d,seq,ts,marker,payload))
    packets=[]
    for key, rows in candidates.items():
        if len(rows) < min_packets or not _sequence_plausible([r[1] for r in rows]):
            continue
        for d,seq,ts,marker,payload in rows:
            packets.append(NormalizedPacket(
                frame_number=d.frame_number,timestamp=d.timestamp,src_ip=d.src_ip,dst_ip=d.dst_ip,
                transport='UDP',src_port=d.src_port,dst_port=d.dst_port,protocols=['rtp'],
                rtp=RtpData(ssrc=key[4],sequence=seq,timestamp=ts,payload_type=key[5],marker=marker,
                            payload_size=len(payload),payload_hex=payload.hex()),raw_fields={'fallback':True}
            ))
    return sorted(packets,key=lambda p:(p.timestamp,p.frame_number))


def _parse_rtp(data: bytes):
    if len(data) < 12 or (data[0] >> 6) != 2:
        return None
    padding=(data[0] >> 5) & 1; extension=(data[0] >> 4) & 1; cc=data[0] & 0x0F
    offset=12+cc*4
    if len(data) < offset:
        return None
    if extension:
        if len(data) < offset+4: return None
        ext_words=struct.unpack('!H',data[offset+2:offset+4])[0]
        offset += 4 + ext_words*4
        if len(data) < offset: return None
    end=len(data)
    if padding:
        pad=data[-1]
        if pad == 0 or pad > end-offset: return None
        end -= pad
    if end <= offset:
        return None
    seq=struct.unpack('!H',data[2:4])[0]; ts=struct.unpack('!I',data[4:8])[0]; ssrc=struct.unpack('!I',data[8:12])[0]
    pt=data[1]&0x7F; marker=bool(data[1]&0x80)
    return seq,ts,ssrc,pt,marker,data[offset:end]


def _sequence_plausible(seqs: list[int]) -> bool:
    if len(seqs) < 2: return False
    good=0; prev=seqs[0]
    for cur in seqs[1:]:
        delta=(cur-prev)&0xFFFF
        if delta in {0,1} or 1 < delta <= 20 or delta >= 65516: good += 1
        prev=cur
    return good/max(1,len(seqs)-1) >= 0.9
