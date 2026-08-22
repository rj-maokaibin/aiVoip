from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .rtp import RtpPtimeHint, RtpStreamAnalyzer
from .rtcp import analyze_rtcp
from .sdp import merge_payload_maps, parse_sdp
from .sip import reconstruct_sip
from .tshark import TSharkAdapter
from .types import NormalizedPacket
from app.analyzers.profile import get_default_analyzer_profile


class PacketIntelligenceEngine:
    analyzer_name = "packet_intelligence"
    analyzer_version = "0.5.0"

    def __init__(self, tshark: TSharkAdapter | None = None):
        self.tshark = tshark or TSharkAdapter()
        self.analyzer_profile = get_default_analyzer_profile()
        self.rtp_config = self.analyzer_profile.section("rtp")

    def analyze_pcap(self, path: str | Path) -> dict:
        packets = list(self.tshark.iter_packets(path))
        result = self.analyze_packets(packets)
        result["source"] = {
            "type": "pcap",
            "tshark_version": self.tshark.version(),
        }
        return result

    def analyze_packets(self, packets: Iterable[NormalizedPacket]) -> dict:
        packets = sorted(list(packets), key=lambda p: (p.timestamp, p.frame_number))
        sip = reconstruct_sip(packets)
        parsed_sdps = [parsed for p in packets if p.sdp and (parsed := parse_sdp(p.sdp))]
        payload_map = merge_payload_maps(parsed_sdps)
        ptime_hints = self._build_ptime_hints(packets)
        rtp_streams = RtpStreamAnalyzer(payload_map, ptime_hints).analyze(packets)
        rtcp_reports = analyze_rtcp(packets)
        self._attach_streams_to_calls(sip["calls"], rtp_streams)
        self._attach_stream_call_bindings(sip["calls"], rtp_streams)
        self._attach_media_direction_health(sip["calls"], rtp_streams)
        anomalies = self._collect_anomalies(sip, rtp_streams)
        return {
            "analyzer": self.analyzer_name,
            "version": self.analyzer_version,
            "status": "SUCCESS",
            "analyzer_profile": self.analyzer_profile.metadata(),
            "summary": {
                "packet_count": len(packets),
                "sip_message_count": sip["sip_message_count"],
                "registration_count": len(sip["registrations"]),
                "call_count": len(sip["calls"]),
                "rtp_stream_count": len(rtp_streams),
                "rtcp_report_count": len(rtcp_reports),
                "anomaly_count": len(anomalies),
            },
            "registrations": sip["registrations"],
            "calls": sip["calls"],
            "rtp_streams": rtp_streams,
            "rtcp_reports": rtcp_reports,
            "anomalies": anomalies,
        }

    @staticmethod
    def _build_ptime_hints(packets: list[NormalizedPacket]) -> list[RtpPtimeHint]:
        hints: list[RtpPtimeHint] = []
        for packet in packets:
            if not packet.sdp:
                continue
            parsed = parse_sdp(packet.sdp)
            if parsed is None:
                continue
            for media in parsed.media:
                if media.media != "audio" or media.ptime_ms is None or not media.connection_address or not media.port:
                    continue
                hints.append(RtpPtimeHint(
                    timestamp=float(packet.timestamp),
                    ip=str(media.connection_address),
                    port=int(media.port),
                    payload_types=tuple(int(x) for x in media.payload_types),
                    ptime_ms=float(media.ptime_ms),
                ))
        return hints

    def _attach_streams_to_calls(self, calls: list[dict], streams: list[dict]) -> None:
        for call in calls:
            endpoints = set()
            for which in ("offer", "answer"):
                sdp = call.get("sdp", {}).get(which)
                if not sdp:
                    continue
                for media in sdp.get("media", []):
                    if media.get("media") == "audio" and media.get("connection_address") and media.get("port"):
                        endpoints.add((media["connection_address"], media["port"]))
            matches = []
            for stream in streams:
                src = (stream.get("src_ip"), stream.get("src_port"))
                dst = (stream.get("dst_ip"), stream.get("dst_port"))
                if (src in endpoints or dst in endpoints) and self._stream_overlaps_call(stream, call):
                    matches.append(stream["stream_id"])
            call["rtp_stream_ids"] = matches
            negotiated = {x["name"].upper() for x in call.get("sdp", {}).get("negotiated_codecs", [])}
            actual = {s["codec"].upper() for s in streams if s["stream_id"] in matches and s.get("codec")}
            mismatches = sorted(actual - negotiated) if negotiated else []
            call["sdp"]["actual_rtp_codecs"] = sorted(actual)
            call["sdp"]["codec_mismatch"] = bool(mismatches)
            call["sdp"]["unexpected_actual_codecs"] = mismatches

    @staticmethod
    def _call_audio_endpoints(call: dict) -> tuple[tuple[str, int] | None, tuple[str, int] | None]:
        sdp = call.get("sdp") or {}
        offer = sdp.get("offer") or {}
        answer = sdp.get("answer") or {}
        offer_media = next((m for m in offer.get("media", []) or [] if m.get("media") == "audio" and m.get("connection_address") and m.get("port")), None)
        answer_media = next((m for m in answer.get("media", []) or [] if m.get("media") == "audio" and m.get("connection_address") and m.get("port")), None)
        a = (str(offer_media["connection_address"]), int(offer_media["port"])) if offer_media else None
        b = (str(answer_media["connection_address"]), int(answer_media["port"])) if answer_media else None
        return a, b

    def _attach_stream_call_bindings(self, calls: list[dict], streams: list[dict]) -> None:
        """Attach explicit SIP-call and directional role metadata to each RTP stream.

        A B2BUA capture may contain multiple call legs. The packet analyzer preserves
        every raw leg and records all compatible call bindings instead of guessing a
        single diagnostic DUT call. PR3's subject-call selector is responsible for the
        DUT-facing choice when PCM provenance is available.
        """
        by_id = {str(stream.get("stream_id")): stream for stream in streams}
        for stream in streams:
            stream["call_bindings"] = []
            stream["primary_call_id"] = None
            stream["call_direction_role"] = "UNBOUND"
        for call in calls:
            a, b = self._call_audio_endpoints(call)
            for stream_id in call.get("rtp_stream_ids", []) or []:
                stream = by_id.get(str(stream_id))
                if not stream:
                    continue
                src = (str(stream.get("src_ip")), int(stream.get("src_port"))) if stream.get("src_ip") and stream.get("src_port") is not None else None
                dst = (str(stream.get("dst_ip")), int(stream.get("dst_port"))) if stream.get("dst_ip") and stream.get("dst_port") is not None else None
                if a and b and src == a and dst == b:
                    role = "OFFERER_TO_ANSWERER"
                elif a and b and src == b and dst == a:
                    role = "ANSWERER_TO_OFFERER"
                else:
                    role = "CALL_ASSOCIATED_OTHER"
                stream["call_bindings"].append({
                    "call_id": call.get("call_id"),
                    "direction_role": role,
                    "offer_audio_endpoint": {"ip": a[0], "port": a[1]} if a else None,
                    "answer_audio_endpoint": {"ip": b[0], "port": b[1]} if b else None,
                })
        for stream in streams:
            bindings = stream.get("call_bindings") or []
            if len(bindings) == 1:
                stream["primary_call_id"] = bindings[0].get("call_id")
                stream["call_direction_role"] = bindings[0].get("direction_role") or "CALL_ASSOCIATED_OTHER"
            elif len(bindings) > 1:
                stream["call_direction_role"] = "MULTI_CALL_BOUND"

    def _stream_overlaps_call(self, stream: dict, call: dict, tolerance_seconds: float | None = None) -> bool:
        tolerance_seconds = float(self.rtp_config["call_scope_tolerance_seconds"]) if tolerance_seconds is None else tolerance_seconds
        start = call.get("media_start_time")
        end = call.get("media_end_time")
        if start is None:
            start = call.get("start_time")
        if end is None:
            end = call.get("end_time")
        ss = stream.get("start_time")
        se = stream.get("end_time")
        if None in {start, end, ss, se}:
            return True
        return float(se) >= float(start) - tolerance_seconds and float(ss) <= float(end) + tolerance_seconds

    @staticmethod
    def _direction_packet_accounting(scoped: list[dict], src: tuple[str, int], dst: tuple[str, int]) -> dict[str, int]:
        """Return RTP packet accounting for one negotiated media direction.

        ``packet_count`` is the number of observed RTP datagrams and therefore includes
        duplicates. Media-health decisions must be based on unique/effective sequence
        packets so retransmitted/duplicated datagrams cannot manufacture media presence
        or inflate the meaningful-media threshold. Raw observations and duplicates are
        preserved separately for evidence/reporting.
        """
        matched = [
            stream for stream in scoped
            if (stream.get("src_ip"), stream.get("src_port")) == src
            and (stream.get("dst_ip"), stream.get("dst_port")) == dst
        ]
        observed = sum(int(stream.get("packet_count") or 0) for stream in matched)
        unique = sum(int(stream.get("unique_packet_count", stream.get("packet_count", 0)) or 0) for stream in matched)
        duplicates = sum(int(stream.get("duplicate_packets") or 0) for stream in matched)
        return {"observed": observed, "unique": unique, "duplicates": duplicates}

    def _attach_media_direction_health(self, calls: list[dict], streams: list[dict]) -> None:
        """Derive call-scoped RTP direction completeness from negotiated audio endpoints.

        This is deliberately conservative: a ONE_WAY_RTP_MEDIA anomaly is only eligible
        when SIP INVITE/2xx/ACK capture completeness is good, both audio endpoints are
        known, SDP expects bidirectional media, and one direction has a meaningful RTP
        stream while the reverse direction is entirely absent.

        Directional media presence is classified with unique RTP sequence packets, not
        raw observed datagrams. Duplicate packets remain evidence but do not count as
        additional effective media packets.
        """
        min_packets = int(self.rtp_config["one_way_min_packets"])
        for call in calls:
            health = {
                "eligible": False,
                "expected_bidirectional": False,
                "endpoint_a": None,
                "endpoint_b": None,
                "packet_count_semantics": "UNIQUE_EFFECTIVE_RTP_PACKETS",
                "a_to_b_packets": 0,
                "b_to_a_packets": 0,
                "a_to_b_observed_packets": 0,
                "b_to_a_observed_packets": 0,
                "a_to_b_duplicate_packets": 0,
                "b_to_a_duplicate_packets": 0,
                "status": "UNKNOWN",
                "reason": None,
            }
            call["media_direction_health"] = health
            if call.get("state") not in {"ESTABLISHED", "TERMINATED"}:
                health["reason"] = "CALL_NOT_ESTABLISHED"
                continue
            if (call.get("capture_completeness") or {}).get("is_partial"):
                health["reason"] = "SIP_CAPTURE_PARTIAL"
                continue
            offer = (call.get("sdp") or {}).get("offer") or {}
            answer = (call.get("sdp") or {}).get("answer") or {}
            om = next((m for m in offer.get("media", []) if m.get("media") == "audio" and m.get("connection_address") and m.get("port")), None)
            am = next((m for m in answer.get("media", []) if m.get("media") == "audio" and m.get("connection_address") and m.get("port")), None)
            if not om or not am:
                health["reason"] = "AUDIO_ENDPOINTS_INCOMPLETE"
                continue
            expected = (om.get("direction", "sendrecv") == "sendrecv" and am.get("direction", "sendrecv") == "sendrecv")
            health["expected_bidirectional"] = expected
            if not expected:
                health["reason"] = "SDP_NOT_BIDIRECTIONAL"
                continue
            a = (om["connection_address"], int(om["port"]))
            b = (am["connection_address"], int(am["port"]))
            health["endpoint_a"] = {"ip": a[0], "port": a[1]}
            health["endpoint_b"] = {"ip": b[0], "port": b[1]}
            matched_ids = set(call.get("rtp_stream_ids") or [])
            scoped = [s for s in streams if s.get("stream_id") in matched_ids]
            a_to_b = self._direction_packet_accounting(scoped, a, b)
            b_to_a = self._direction_packet_accounting(scoped, b, a)
            a_to_b_effective = a_to_b["unique"]
            b_to_a_effective = b_to_a["unique"]
            health.update({
                "eligible": True,
                "a_to_b_packets": a_to_b_effective,
                "b_to_a_packets": b_to_a_effective,
                "a_to_b_observed_packets": a_to_b["observed"],
                "b_to_a_observed_packets": b_to_a["observed"],
                "a_to_b_duplicate_packets": a_to_b["duplicates"],
                "b_to_a_duplicate_packets": b_to_a["duplicates"],
            })
            if a_to_b_effective >= min_packets and b_to_a_effective >= min_packets:
                health["status"] = "BIDIRECTIONAL"
            elif a_to_b_effective >= min_packets and b_to_a_effective == 0:
                health["status"] = "ONE_WAY_A_TO_B"
            elif b_to_a_effective >= min_packets and a_to_b_effective == 0:
                health["status"] = "ONE_WAY_B_TO_A"
            else:
                health["status"] = "INSUFFICIENT_MEDIA"
                health["reason"] = "NOT_ENOUGH_RTP_TO_ASSERT_ONE_WAY"

    def _collect_anomalies(self, sip: dict, streams: list[dict]) -> list[dict]:
        anomalies: list[dict] = []
        for reg in sip["registrations"]:
            if reg["status"] == "FAILED":
                anomalies.append({
                    "type": "SIP_REGISTRATION_FAILED",
                    "severity": "HIGH",
                    "time": reg["end_time"],
                    "evidence": {"call_id": reg["call_id"], "final_status_code": reg["final_status_code"]},
                })
        for call in sip["calls"]:
            if call["state"] == "FAILED":
                anomalies.append({
                    "type": "SIP_CALL_FAILED",
                    "severity": "HIGH",
                    "time": call["end_time"],
                    "evidence": {"call_id": call["call_id"], "final_status_code": call["invite_final_status"]},
                })
            if call.get("conflicting_final_responses"):
                anomalies.append({
                    "type": "SIP_CONFLICTING_FINAL_RESPONSE",
                    "severity": "MEDIUM",
                    "time": call["conflicting_final_responses"][0]["timestamp"],
                    "evidence": {
                        "call_id": call["call_id"],
                        "accepted_final_status": call.get("invite_final_status"),
                        "later_responses": call["conflicting_final_responses"],
                        "note": "可能是分叉/多腿/部分抓包或异常晚到响应，需要结合 Via branch 与抓包完整性解释",
                    },
                })
            media_health = call.get("media_direction_health") or {}
            if media_health.get("status") in {"ONE_WAY_A_TO_B", "ONE_WAY_B_TO_A"}:
                anomalies.append({
                    "type": "ONE_WAY_RTP_MEDIA",
                    "severity": "HIGH",
                    "time": call.get("media_start_time") or call["start_time"],
                    "evidence": {
                        "call_id": call["call_id"],
                        "status": media_health.get("status"),
                        "endpoint_a": media_health.get("endpoint_a"),
                        "endpoint_b": media_health.get("endpoint_b"),
                        "packet_count_semantics": media_health.get("packet_count_semantics"),
                        "a_to_b_packets": media_health.get("a_to_b_packets"),
                        "b_to_a_packets": media_health.get("b_to_a_packets"),
                        "a_to_b_observed_packets": media_health.get("a_to_b_observed_packets"),
                        "b_to_a_observed_packets": media_health.get("b_to_a_observed_packets"),
                        "a_to_b_duplicate_packets": media_health.get("a_to_b_duplicate_packets"),
                        "b_to_a_duplicate_packets": media_health.get("b_to_a_duplicate_packets"),
                        "note": "SDP期望双向媒体且SIP抓包完整；当前仅检测到一个方向的持续RTP。方向有效包数按唯一 RTP Sequence 计数，重复包只作为独立证据保留，不用于制造媒体存在。该事实确认单向RTP现象，但不能单独确认网络、DSP、NAT/PBX中的具体根因。",
                    },
                })
            if call.get("sdp", {}).get("codec_mismatch"):
                anomalies.append({
                    "type": "CODEC_NEGOTIATION_MISMATCH",
                    "severity": "HIGH",
                    "time": call["start_time"],
                    "evidence": {
                        "call_id": call["call_id"],
                        "negotiated": [x["name"] for x in call["sdp"]["negotiated_codecs"]],
                        "actual": call["sdp"]["actual_rtp_codecs"],
                    },
                })
        for stream in streams:
            stream_context = {
                "stream_id": stream.get("stream_id"),
                "call_id": stream.get("primary_call_id"),
                "call_bindings": stream.get("call_bindings") or [],
                "call_direction_role": stream.get("call_direction_role"),
                "src_ip": stream.get("src_ip"),
                "src_port": stream.get("src_port"),
                "dst_ip": stream.get("dst_ip"),
                "dst_port": stream.get("dst_port"),
                "ssrc": stream.get("ssrc"),
                "codec": stream.get("codec"),
                "ptime_ms": stream.get("ptime_ms"),
                "lost_packets": stream.get("lost_packets"),
                "loss_rate": stream.get("loss_rate"),
            }
            for event in stream["events"]:
                anomalies.append({
                    "type": event["type"],
                    "severity": event["severity"],
                    "time": event["start_time"],
                    "evidence": {**stream_context, **event["details"]},
                })
        anomalies.sort(key=lambda x: x["time"])
        return anomalies
