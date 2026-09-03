from __future__ import annotations

import os
import shutil
import socket
import struct
from pathlib import Path

import pytest

from app.analyzers.packet import PacketIntelligenceEngine, TSharkAdapter
from app.reports.v2.runtime_adapter import compose_v2_runtime_from_analyzers


CALLER_IP = "10.30.0.10"
PBX_IP = "10.30.0.20"
CALLER_RTP_PORT = 4000
PBX_RTP_PORT = 5000


def _require_tshark() -> None:
    if shutil.which("tshark"):
        return
    hard_required = os.getenv("REQUIRE_PRELIMINARY_EVIDENCE_V2_PCAP_E2E") == "1" or os.getenv("CI", "").lower() == "true"
    if hard_required:
        pytest.fail("Preliminary Evidence V2 3-PCAP E2E requires TShark on CI/controlled acceptance")
    pytest.skip("TShark is unavailable in this local environment")


def _ether_ipv4_udp(src: str, dst: str, sp: int, dp: int, payload: bytes) -> bytes:
    eth = bytes.fromhex("00112233445566778899aabb0800")
    udp_len = 8 + len(payload)
    total = 20 + udp_len
    ip = bytearray(20)
    ip[0] = 0x45
    struct.pack_into("!H", ip, 2, total)
    ip[8] = 64
    ip[9] = 17
    ip[12:16] = socket.inet_aton(src)
    ip[16:20] = socket.inet_aton(dst)
    udp = struct.pack("!HHHH", sp, dp, udp_len, 0)
    return eth + bytes(ip) + udp + payload


def _write_pcap(path: Path, packets: list[tuple[float, bytes]]) -> Path:
    with path.open("wb") as handle:
        handle.write(b"\xd4\xc3\xb2\xa1")
        handle.write(struct.pack("<HHiiii", 2, 4, 0, 0, 262144, 1))
        for ts, data in sorted(packets, key=lambda item: item[0]):
            sec = int(ts)
            usec = int(round((ts - sec) * 1_000_000))
            if usec >= 1_000_000:
                sec += 1
                usec -= 1_000_000
            handle.write(struct.pack("<IIII", sec, usec, len(data), len(data)))
            handle.write(data)
    return path


def _sdp(ip: str, port: int) -> str:
    return (
        "v=0\r\n"
        f"o=- 1 1 IN IP4 {ip}\r\n"
        "s=voip-evidence-v2\r\n"
        f"c=IN IP4 {ip}\r\n"
        "t=0 0\r\n"
        f"m=audio {port} RTP/AVP 0\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        "a=ptime:20\r\n"
        "a=sendrecv\r\n"
    )


def _sip_request(method: str, call_id: str, cseq: int, *, body: str = "", to_tag: str | None = None) -> bytes:
    target = f"sip:101@{PBX_IP}"
    headers = [
        f"{method} {target} SIP/2.0",
        f"Via: SIP/2.0/UDP {CALLER_IP}:5060;branch=z9hG4bK-{method.lower()}-{cseq}",
        f"From: <sip:601@{CALLER_IP}>;tag=caller-tag",
        f"To: <sip:101@{PBX_IP}>" + (f";tag={to_tag}" if to_tag else ""),
        f"Call-ID: {call_id}",
        f"CSeq: {cseq} {method}",
        f"Contact: <sip:601@{CALLER_IP}:5060>",
        "Max-Forwards: 70",
    ]
    if body:
        headers.append("Content-Type: application/sdp")
    headers.append(f"Content-Length: {len(body.encode())}")
    return ("\r\n".join(headers) + "\r\n\r\n" + body).encode()


def _sip_200_invite(call_id: str, body: str) -> bytes:
    headers = [
        "SIP/2.0 200 OK",
        f"Via: SIP/2.0/UDP {CALLER_IP}:5060;branch=z9hG4bK-invite-1",
        f"From: <sip:601@{CALLER_IP}>;tag=caller-tag",
        f"To: <sip:101@{PBX_IP}>;tag=pbx-tag",
        f"Call-ID: {call_id}",
        "CSeq: 1 INVITE",
        f"Contact: <sip:101@{PBX_IP}:5060>",
        "Content-Type: application/sdp",
        f"Content-Length: {len(body.encode())}",
    ]
    return ("\r\n".join(headers) + "\r\n\r\n" + body).encode()


def _rtp_frame(seq: int, timestamp: int, *, ssrc: int) -> bytes:
    header = struct.pack("!BBHII", 0x80, 0x00, seq & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc)
    return header + (b"\xff" * 160)


def _scenario_pcap(path: Path, *, loss: bool, bye: bool) -> Path:
    call_id = f"evidence-v2-{'loss' if loss else 'normal'}-{'bye' if bye else 'open'}"
    records: list[tuple[float, bytes]] = []
    records.append(
        (1.000, _ether_ipv4_udp(CALLER_IP, PBX_IP, 5060, 5060, _sip_request("INVITE", call_id, 1, body=_sdp(CALLER_IP, CALLER_RTP_PORT))))
    )
    records.append(
        (1.180, _ether_ipv4_udp(PBX_IP, CALLER_IP, 5060, 5060, _sip_200_invite(call_id, _sdp(PBX_IP, PBX_RTP_PORT))))
    )
    records.append(
        (1.200, _ether_ipv4_udp(CALLER_IP, PBX_IP, 5060, 5060, _sip_request("ACK", call_id, 1, to_tag="pbx-tag")))
    )

    for index in range(30):
        # The loss scenario removes three consecutive sequence numbers from the
        # caller->PBX stream while preserving the RTP media clock. The analyzer
        # must surface sequence loss, not reinterpret the gap as timing-only.
        if not (loss and 10 <= index <= 12):
            records.append(
                (
                    1.300 + index * 0.020,
                    _ether_ipv4_udp(
                        CALLER_IP,
                        PBX_IP,
                        CALLER_RTP_PORT,
                        PBX_RTP_PORT,
                        _rtp_frame(1000 + index, index * 160, ssrc=0x11111111),
                    ),
                )
            )
        records.append(
            (
                1.301 + index * 0.020,
                _ether_ipv4_udp(
                    PBX_IP,
                    CALLER_IP,
                    PBX_RTP_PORT,
                    CALLER_RTP_PORT,
                    _rtp_frame(2000 + index, index * 160, ssrc=0x22222222),
                ),
            )
        )

    if bye:
        records.append(
            (2.100, _ether_ipv4_udp(CALLER_IP, PBX_IP, 5060, 5060, _sip_request("BYE", call_id, 2, to_tag="pbx-tag")))
        )
    return _write_pcap(path, records)


def _analyze(tmp_path: Path, *, loss: bool, bye: bool):
    _require_tshark()
    pcap = _scenario_pcap(tmp_path / f"scenario-{loss}-{bye}.pcap", loss=loss, bye=bye)
    packet = PacketIntelligenceEngine(TSharkAdapter()).analyze_pcap(pcap)
    assert packet["summary"]["call_count"] == 1
    assert packet["summary"]["rtp_stream_count"] == 2
    selected_call = packet["calls"][0]
    report = compose_v2_runtime_from_analyzers(
        report_id=f"PCAP-E2E-{int(loss)}-{int(bye)}",
        sip_call=selected_call,
        packet=packet,
        pcm={},
        media={},
        subject_device_ip=CALLER_IP,
    )
    return packet, selected_call, report


def test_pcap_e2e_established_zero_loss_without_bye(tmp_path: Path):
    packet, _call, report = _analyze(tmp_path, loss=False, bye=False)

    assert all(int(stream.get("lost_packets") or 0) == 0 for stream in packet["rtp_streams"])
    assert report["call_reconstruction"]["state"] == "ESTABLISHED"
    assert report["call_reconstruction"]["termination"]["observed"] is False
    assert report["call_reconstruction"]["call_end_time"] is None
    media_window = report["timeline"]["media_observation_window"]
    assert media_window["source"] == "RTP_OBSERVATION"
    assert media_window["end"] > media_window["start"]
    assert not any(item.get("type") == "RTP_SEQUENCE_LOSS" for item in report["findings"])
    assert report["semantic_validation"]["status"] == "PASS"
    assert report["publishable"] is True


def test_pcap_e2e_sequence_loss_is_reported_as_loss(tmp_path: Path):
    packet, _call, report = _analyze(tmp_path, loss=True, bye=False)

    assert max(int(stream.get("lost_packets") or 0) for stream in packet["rtp_streams"]) >= 3
    loss_findings = [item for item in report["findings"] if item.get("type") == "RTP_SEQUENCE_LOSS"]
    assert loss_findings
    assert any(int((item.get("metrics") or {}).get("lost_packets") or 0) >= 3 for item in loss_findings)
    assert report["problem_count"] >= 1
    assert report["semantic_validation"]["status"] == "PASS"
    assert report["publishable"] is True


def test_pcap_e2e_bye_is_the_observed_protocol_termination(tmp_path: Path):
    _packet, _call, report = _analyze(tmp_path, loss=False, bye=True)

    assert report["call_reconstruction"]["state"] == "TERMINATED"
    assert report["call_reconstruction"]["termination"]["observed"] is True
    assert report["call_reconstruction"]["termination"]["type"] == "BYE"
    assert report["call_reconstruction"]["call_end_time"] == pytest.approx(2.100, abs=1e-6)
    media_window = report["timeline"]["media_observation_window"]
    assert media_window["source"] == "RTP_OBSERVATION"
    assert media_window["end"] < report["call_reconstruction"]["call_end_time"]
    assert report["semantic_validation"]["status"] == "PASS"
    assert report["publishable"] is True
