from __future__ import annotations

import hashlib
import html
import json
from datetime import datetime, timezone
from typing import Any

from app.contracts.evidence_report import REPORT_COMPOSER_VERSION, REPORT_SCHEMA_VERSION
from app.reports.finding_composer import build_normal_evidence, compose_findings


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def environment_fingerprint(environment: dict | None) -> str | None:
    return canonical_hash(environment) if environment else None


def build_completeness(*, evidences: list[dict], analyzer_states: dict[str, dict], scope_type: str) -> dict:
    def available(name: str) -> dict:
        item = analyzer_states.get(name) or {}
        status = str(item.get("status") or "UNAVAILABLE")
        return {
            "status": status,
            "available": status in {"SUCCESS", "PARTIAL_SUCCESS"},
            "partial": status == "PARTIAL_SUCCESS",
            "reason": item.get("error_code") or item.get("error_message") or item.get("degraded_reason"),
        }

    evidence_types = {str(x.get("type") or "").upper() for x in evidences}
    packet = available("packet_intelligence")
    pcm = available("pcm_intelligence")
    media = available("media_intelligence")
    capture = {
        "pcap": any(x in evidence_types for x in {"PCAP", "PCAPNG"}),
        "pcm_rx": any("PCM_RX" in x for x in evidence_types) or pcm["available"] or media["available"],
        "pcm_tx": any("PCM_TX" in x for x in evidence_types) or pcm["available"] or media["available"],
        "debug": any("DEBUG" in x or "LOG" in x for x in evidence_types),
    }
    missing = [name for name, present in capture.items() if name in {"pcap", "pcm_rx", "pcm_tx"} and not present]
    unavailable = [name for name, item in {"packet": packet, "pcm": pcm, "media": media}.items() if not item["available"]]
    state = "COMPLETE" if not missing and not unavailable else "PARTIAL"
    return {
        "state": state,
        "scope_type": scope_type,
        "capture": capture,
        "analyzers": {"packet": packet, "pcm": pcm, "media": media},
        "missing_required_evidence": missing,
        "unavailable_analyzers": unavailable,
        "boundary": (
            "证据完整，可进行当前范围的 Packet/PCM/Media 初步证据分析。"
            if state == "COMPLETE" else
            "部分证据或 Analyzer 不可用；报告仍可生成，但缺失方向不得用于排除或根因确认。"
        ),
    }


def build_packet_summary(packet: dict | None) -> dict:
    if not packet:
        return {"available": False}
    summary = packet.get("summary") or {}
    streams = []
    for stream in packet.get("rtp_streams", []) or []:
        streams.append({
            "stream_id": stream.get("stream_id"),
            "source": f"{stream.get('src_ip')}:{stream.get('src_port')}",
            "destination": f"{stream.get('dst_ip')}:{stream.get('dst_port')}",
            "ssrc": stream.get("ssrc"),
            "packet_count": stream.get("packet_count"),
            "lost_packets": stream.get("lost_packets", stream.get("lost")),
            "loss_rate": stream.get("loss_rate"),
            "avg_jitter_ms": stream.get("avg_rfc3550_jitter_ms"),
            "p95_jitter_ms": stream.get("p95_rfc3550_jitter_ms"),
            "max_jitter_ms": stream.get("max_rfc3550_jitter_ms"),
            "max_delta_ms": stream.get("max_delta_ms"),
            "codec": stream.get("codec"),
            "ptime_ms": stream.get("ptime_ms"),
        })
    return {
        "available": True,
        "packet_count": summary.get("packet_count"),
        "sip_message_count": summary.get("sip_message_count"),
        "call_count": summary.get("call_count"),
        "rtp_stream_count": summary.get("rtp_stream_count"),
        "rtcp_report_count": summary.get("rtcp_report_count"),
        "streams": streams,
        "calls": packet.get("calls", []),
    }


def build_pcm_summary(pcm: dict | None) -> dict:
    if not pcm:
        return {"available": False}
    streams = []
    for stream in pcm.get("streams", []) or []:
        tap = stream.get("tap") or {}
        sessions = []
        for session in stream.get("sessions", []) or []:
            signal = session.get("signal") or {}
            sessions.append({
                "session_index": session.get("session_index"),
                "start_time": session.get("start_time"),
                "end_time": session.get("end_time"),
                "packet_count": session.get("packet_count"),
                "audio_duration_seconds": session.get("audio_duration_seconds"),
                "gap_event_count": session.get("gap_event_count"),
                "rms_dbfs": signal.get("rms_dbfs", signal.get("dbfs")),
                "peak_dbfs": signal.get("peak_dbfs"),
                "peak_amplitude": signal.get("peak"),
                "dc_offset": signal.get("dc_offset"),
                "clipping_percent": signal.get("clipping_percent"),
                "hum": session.get("hum"),
                "spectral": session.get("spectral"),
                "silence_event_count": len(session.get("silence_events", []) or []),
                "click_pop_event_count": len(session.get("click_pop_events", []) or []),
                "dtmf_sequences": session.get("dtmf_sequences", []),
            })
        streams.append({"tap": tap, "packet_count": stream.get("packet_count"), "sessions": sessions})
    return {
        "available": True,
        "summary": pcm.get("summary") or {},
        "format": pcm.get("format") or {},
        "level_definition": {
            "unit": "dBFS",
            "rms": "RMS dBFS：数字 PCM 音频均方根电平，相对于数字满量程。",
            "peak": "Peak dBFS：数字 PCM 音频峰值电平，相对于数字满量程。",
            "boundary": "dBFS 不等于实际声压级 dB SPL；未校准的数字 PCM 不能声称真实声压分贝。",
        },
        "streams": streams,
    }


def build_report_payload(*, case: dict, scope_type: str, scope_id: str,
                         session: dict | None, call: dict | None,
                         environment: dict | None, evidences: list[dict],
                         analyzer_states: dict[str, dict], results: dict[str, dict | None],
                         report_version: int, generated_at: str | None = None) -> dict:
    packet = results.get("packet_intelligence")
    pcm = results.get("pcm_intelligence")
    media = results.get("media_intelligence")
    source_run_ids = {name: state.get("run_id") for name, state in analyzer_states.items() if state.get("run_id")}
    findings = compose_findings(packet=packet, pcm=pcm, media=media, source_run_ids=source_run_ids)
    completeness = build_completeness(evidences=evidences, analyzer_states=analyzer_states, scope_type=scope_type)
    normal = build_normal_evidence(packet, pcm, media)
    highest = findings[0]["severity"] if findings else "INFO"
    headline = (
        f"发现 {len(findings)} 个初步证据问题点，最高等级 {highest}。"
        if findings else "当前已完成的 Analyzer 未发现明显异常；该结论仅覆盖已采集且可分析的证据范围。"
    )
    boundary = {
        "root_cause_authority": "PRELIMINARY_EVIDENCE_ONLY",
        "statement": "本报告描述已观测事实、候选异常和证据边界，不确认最终 Root Cause（根因）。",
        "historical_case_authority": "历史 Case 和 AI 解释不能将当前 Finding 提升为 L1/L2 或独立确认根因。",
    }
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "composer_version": REPORT_COMPOSER_VERSION,
        "report_version": report_version,
        "generated_at": generated_at or utcnow_iso(),
        "scope": {"type": scope_type, "id": scope_id},
        "case": case,
        "session": session,
        "call": call,
        "environment": environment or {},
        "environment_fingerprint": environment_fingerprint(environment),
        "headline": headline,
        "finding_count": len(findings),
        "highest_severity": highest,
        "completeness": completeness,
        "packet_summary": build_packet_summary(packet),
        "pcm_summary": build_pcm_summary(pcm),
        "media_summary": (media or {}).get("summary") if media else None,
        "findings": findings,
        "normal_and_exclusion_evidence": normal,
        "analyzers": analyzer_states,
        "artifacts": [],
        "preliminary_assessment": {
            "summary": headline,
            "evidence_boundary": boundary["statement"],
            "recommended_next_action": "优先复核 HIGH/CRITICAL Finding 对应的时间窗、图像和音频；如需确认物理根因，进入确定性 Diagnosis/A-B/Fix Verification 流程。",
        },
        "evidence_boundary": boundary,
    }
    payload["input_snapshot_hash"] = canonical_hash({
        "scope": payload["scope"], "case": case, "session": session, "call": call,
        "environment": environment, "evidences": evidences, "analyzers": analyzer_states,
        "result_hashes": {k: canonical_hash(v) if v is not None else None for k, v in results.items()},
    })
    return payload


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _metric_table(metrics: dict) -> str:
    if not metrics:
        return "<p>无附加指标。</p>"
    rows = []
    for key, value in list(metrics.items())[:18]:
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        rows.append(f"<tr><td>{_esc(key)}</td><td>{_esc(value)}</td></tr>")
    return "<table><tbody>" + "".join(rows) + "</tbody></table>"


def render_report_html(payload: dict) -> str:
    findings = payload.get("findings", [])
    finding_html = []
    for finding in findings:
        tr = finding.get("time_range") or {}
        finding_html.append(
            f"<section class='finding sev-{_esc(finding.get('severity','INFO')).lower()}'>"
            f"<h3>{_esc(finding.get('severity'))}｜{_esc(finding.get('title'))}</h3>"
            f"<p><b>时间：</b>{_esc(tr.get('start'))} ～ {_esc(tr.get('end'))}　"
            f"<b>证据等级：</b>{_esc(finding.get('evidence_level'))}</p>"
            f"<p><b>已观测事实：</b>{_esc(finding.get('observation'))}</p>"
            f"<p><b>初步解释：</b>{_esc(finding.get('interpretation'))}</p>"
            f"<div class='boundary'><b>根因边界：</b>{_esc(finding.get('root_cause_boundary'))}</div>"
            f"<details><summary>关键指标 / 研发下钻</summary>{_metric_table(finding.get('metrics') or {})}</details>"
            "</section>"
        )
    if not finding_html:
        finding_html.append("<div class='ok'>当前可用证据中未发现明显异常 Finding。</div>")

    comp = payload.get("completeness") or {}
    cap = comp.get("capture") or {}
    capture_rows = "".join(
        f"<tr><td>{_esc(k)}</td><td>{'✅ 可用' if v else '⚠️ 缺失/不可用'}</td></tr>" for k, v in cap.items()
    )
    normal = "".join(f"<li>✅ {_esc(x.get('text'))}</li>" for x in payload.get("normal_and_exclusion_evidence", [])) or "<li>暂无可展示的排除性证据。</li>"
    packet = payload.get("packet_summary") or {}
    stream_rows = "".join(
        f"<tr><td>{_esc(s.get('source'))}</td><td>{_esc(s.get('destination'))}</td><td>{_esc(s.get('packet_count'))}</td>"
        f"<td>{_esc(s.get('lost_packets'))}</td><td>{_esc(s.get('loss_rate'))}</td><td>{_esc(s.get('p95_jitter_ms'))}</td>"
        f"<td>{_esc(s.get('codec'))}</td><td>{_esc(s.get('ptime_ms'))}</td></tr>"
        for s in packet.get("streams", [])
    )
    return f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>
<title>{_esc((payload.get('case') or {}).get('case_no'))} VOIP 初步证据分析报告</title>
<style>
body{{font-family:Arial,'Microsoft YaHei',sans-serif;max-width:1180px;margin:28px auto;color:#1f2937;line-height:1.65;padding:0 18px}}
h1,h2,h3{{color:#111827}}.hero{{padding:18px;border:1px solid #d1d5db;border-radius:12px;background:#f8fafc}}
.badge{{display:inline-block;padding:3px 9px;border-radius:10px;background:#e5e7eb;margin-right:6px}}table{{width:100%;border-collapse:collapse;margin:8px 0}}
th,td{{border:1px solid #d1d5db;padding:7px;vertical-align:top}}th{{background:#f3f4f6}}.finding{{border:1px solid #d1d5db;border-left:5px solid #6b7280;padding:14px;margin:14px 0;border-radius:8px}}
.sev-high,.sev-critical{{border-left-color:#b91c1c}}.sev-medium{{border-left-color:#d97706}}.boundary{{background:#fff7ed;padding:10px;border-radius:6px}}.ok{{background:#ecfdf5;padding:12px;border-radius:8px}}
.small{{color:#6b7280;font-size:13px}}
</style></head><body>
<h1>VOIP 初步证据分析报告</h1>
<div class='hero'><span class='badge'>{_esc((payload.get('case') or {}).get('case_no'))}</span><span class='badge'>{_esc(payload.get('scope',{}).get('type'))}</span><span class='badge'>Report V{_esc(payload.get('report_version'))}</span>
<h2>{_esc(payload.get('headline'))}</h2><p>{_esc((payload.get('case') or {}).get('summary'))}</p>
<p class='small'>专业术语首次出现说明：RTP（Real-time Transport Protocol，实时传输协议）；PCM（Pulse Code Modulation，脉冲编码调制）；dBFS（Decibels relative to Full Scale，相对于数字满量程的分贝）。</p></div>
<h2>1. 当前初步结论</h2><p>{_esc((payload.get('preliminary_assessment') or {}).get('summary'))}</p><div class='boundary'>{_esc((payload.get('evidence_boundary') or {}).get('statement'))}</div>
<h2>2. 当前重点问题</h2>{''.join(finding_html)}
<h2>3. 证据完整度</h2><p><b>{_esc(comp.get('state'))}</b>：{_esc(comp.get('boundary'))}</p><table>{capture_rows}</table>
<h2>4. 网络媒体关键指标</h2><p>总帧/包数：{_esc(packet.get('packet_count'))}；SIP 消息：{_esc(packet.get('sip_message_count'))}；Call：{_esc(packet.get('call_count'))}；RTP Stream：{_esc(packet.get('rtp_stream_count'))}</p>
<table><thead><tr><th>源</th><th>目的</th><th>包数</th><th>丢包</th><th>丢包率</th><th>P95 抖动(ms)</th><th>Codec</th><th>ptime(ms)</th></tr></thead><tbody>{stream_rows}</tbody></table>
<h2>5. 正常项 / 排除性证据</h2><ul>{normal}</ul>
<h2>6. 音频电平口径</h2><p>RMS dBFS 为数字 PCM 平均电平，Peak dBFS 为数字 PCM 峰值电平；二者均不等价于真实声压级 dB SPL。</p>
<h2>7. 证据下钻</h2><p>图像、异常音频、Frame/Analyzer JSON 与 Evidence Bundle 由同一 Canonical Report JSON 引用；飞书文档/Web UI 仅为展示层。</p>
<hr><p class='small'>Schema: {_esc(payload.get('schema_version'))}；Composer: {_esc(payload.get('composer_version'))}；Generated: {_esc(payload.get('generated_at'))}</p>
</body></html>"""
