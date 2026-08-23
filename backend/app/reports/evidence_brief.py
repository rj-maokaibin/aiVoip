from __future__ import annotations

import hashlib
import html
import json
from datetime import datetime, timezone
from typing import Any

from app.contracts.evidence_report import REPORT_COMPOSER_VERSION, REPORT_SCHEMA_VERSION
from app.reports.evidence_card import attach_evidence_cards
from app.reports.finding_composer import build_normal_evidence, compose_findings


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def environment_fingerprint(environment: dict | None) -> str | None:
    return canonical_hash(environment) if environment else None


def build_completeness(
    *,
    evidences: list[dict],
    analyzer_states: dict[str, dict],
    scope_type: str,
    results: dict[str, dict | None] | None = None,
) -> dict:
    results = results or {}

    def state(name: str) -> dict:
        item = analyzer_states.get(name) or {}
        status = str(item.get("status") or "UNAVAILABLE")
        return {
            "status": status,
            "available": status in {"SUCCESS", "PARTIAL_SUCCESS"},
            "partial": status == "PARTIAL_SUCCESS",
            "reason": item.get("error_code") or item.get("error_message") or item.get("degraded_reason"),
        }

    evidence_types = {str(x.get("type") or "").upper() for x in evidences}
    packet = state("packet_intelligence")
    pcm_state = state("pcm_intelligence")
    media = state("media_intelligence")
    pcm_result = results.get("pcm_intelligence") or {}
    taps = {str((x.get("tap") or {}).get("name") or "").lower() for x in pcm_result.get("streams", []) or []}
    capture = {
        "pcap": any(x in evidence_types for x in {"PCAP", "PCAPNG"}),
        "pcm_rx": ("pcm_rx" in taps) or any("PCM_RX" in x for x in evidence_types),
        "pcm_tx": ("pcm_tx" in taps) or any("PCM_TX" in x for x in evidence_types),
        "debug": any("DEBUG" in x or "LOG" in x for x in evidence_types),
    }
    missing = [name for name in ("pcap", "pcm_rx", "pcm_tx") if not capture[name]]
    analyzers = {"packet": packet, "pcm": pcm_state, "media": media}
    unavailable = [name for name, item in analyzers.items() if not item["available"]]
    state_value = "COMPLETE" if not missing and not unavailable else "PARTIAL"
    return {
        "state": state_value,
        "scope_type": scope_type,
        "capture": capture,
        "analyzers": analyzers,
        "missing_required_evidence": missing,
        "unavailable_analyzers": unavailable,
        "boundary": (
            "证据完整，可进行当前范围的 Packet/PCM/Media 初步证据分析。"
            if state_value == "COMPLETE"
            else "部分证据或 Analyzer 不可用；报告仍可生成，但缺失方向不得用于排除或根因确认。"
        ),
    }


def build_packet_summary(packet: dict | None) -> dict:
    if not packet:
        return {"available": False}
    summary = packet.get("summary") or {}
    streams = []
    for stream in packet.get("rtp_streams", []) or []:
        observed = int(stream.get("packet_count") or 0)
        unique = int(stream.get("unique_packet_count", stream.get("packet_count", 0)) or 0)
        duplicates = int(stream.get("duplicate_packets") or 0)
        streams.append({
            "stream_id": stream.get("stream_id"),
            "source": f"{stream.get('src_ip')}:{stream.get('src_port')}",
            "destination": f"{stream.get('dst_ip')}:{stream.get('dst_port')}",
            "ssrc": stream.get("ssrc"),
            "packet_count_semantics": "UNIQUE_EFFECTIVE_RTP_PACKETS",
            "packet_count": unique,
            "unique_packet_count": unique,
            "observed_packet_count": observed,
            "duplicate_packets": duplicates,
            "expected_packets": stream.get("expected_packets"),
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


def build_report_payload(
    *,
    case: dict,
    scope_type: str,
    scope_id: str,
    session: dict | None,
    call: dict | None,
    environment: dict | None,
    evidences: list[dict],
    analyzer_states: dict[str, dict],
    results: dict[str, dict | None],
    report_version: int,
    generated_at: str | None = None,
    analysis_context: dict | None = None,
    display_call: dict | None = None,
) -> dict:
    packet = results.get("packet_intelligence")
    pcm = results.get("pcm_intelligence")
    media = results.get("media_intelligence")
    source_run_ids = {name: state.get("run_id") for name, state in analyzer_states.items() if state.get("run_id")}
    findings = compose_findings(packet=packet, pcm=pcm, media=media, source_run_ids=source_run_ids)
    completeness = build_completeness(
        evidences=evidences,
        analyzer_states=analyzer_states,
        scope_type=scope_type,
        results=results,
    )
    resolved_call = display_call if display_call is not None else call
    context = analysis_context or {}
    semantic_issues = list(context.get("semantic_issues") or [])
    if semantic_issues:
        completeness["semantic_status"] = "INCOMPLETE"
        completeness["semantic_issues"] = semantic_issues
        completeness["reviewability"] = "NOT_FULLY_REVIEWABLE"
        completeness["state"] = "PARTIAL"
        prior = str(completeness.get("boundary") or "")
        completeness["boundary"] = (
            "Call/媒体上下文存在语义绑定缺口；报告已降级为 PARTIAL_COMPLETE / NOT_FULLY_REVIEWABLE。 " + prior
        ).strip()
    else:
        completeness["semantic_status"] = "OK"
        completeness["semantic_issues"] = []
        completeness["reviewability"] = context.get("reviewability") or "FULLY_REVIEWABLE"
    normal = build_normal_evidence(packet, pcm, media)
    highest = findings[0]["severity"] if findings else "INFO"
    headline = (
        f"发现 {len(findings)} 个初步证据问题点，最高等级 {highest}。"
        if findings
        else "当前已完成的 Analyzer 未发现明显异常；该结论仅覆盖已采集且可分析的证据范围。"
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
        "call": resolved_call,
        "display_call": resolved_call,
        "analysis_context": context,
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
        "scope": payload["scope"],
        "case": case,
        "session": session,
        "display_call": resolved_call,
        "analysis_context": context,
        "environment": environment,
        "evidences": evidences,
        "analyzers": analyzer_states,
        "result_hashes": {key: canonical_hash(value) if value is not None else None for key, value in results.items()},
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


def _multi_call_html(payload: dict) -> str:
    context = payload.get("analysis_context") or {}
    offline = context.get("analysis_mode") == "OFFLINE_IMPORTED"
    summary = payload.get("multi_call_summary") or {}
    groups = summary.get("finding_groups") or []
    offline_note = (
        f"<p>当前为离线证据分析；PCAP 重建 Call 数：{_esc(context.get('reconstructed_call_count'))}。"
        "此处的“有效 Call 报告数/复现率”仅用于 Reproduction Call，不把离线重建 Call 伪计为复现次数。</p>"
        if offline else ""
    )
    if not summary:
        return offline_note or "<p>当前为 Call 级报告，无跨 Call 聚合。</p>"
    rows = "".join(
        f"<tr><td>{_esc(x.get('title'))}</td><td>{_esc(x.get('occurrence_calls'))}/{_esc(x.get('total_calls'))}</td>"
        f"<td>{_esc(round((x.get('reproduction_rate') or 0) * 100, 2))}%</td><td>{_esc(x.get('stability'))}</td></tr>"
        for x in groups
    )
    return offline_note + (
        f"<p>有效 Reproduction Call 报告数：{_esc(summary.get('call_count'))}</p>"
        f"<table><thead><tr><th>问题</th><th>出现</th><th>复现率</th><th>稳定性</th></tr></thead><tbody>{rows}</tbody></table>"
    )


def _ab_html(payload: dict) -> str:
    comparisons = payload.get("ab_comparison") or []
    if not comparisons:
        return "<p>当前没有满足分组条件的 A/B 环境对比。</p>"
    parts = []
    for comp in comparisons:
        rows = "".join(
            f"<tr><td>{_esc(x.get('title'))}</td><td>{_esc(round((x.get('environment_a_rate') or 0) * 100, 2))}%</td>"
            f"<td>{_esc(round((x.get('environment_b_rate') or 0) * 100, 2))}%</td>"
            f"<td>{_esc(round((x.get('absolute_rate_delta') or 0) * 100, 2))}%</td>"
            f"<td>{'是' if x.get('significant_by_v1_rule') else '否'}</td></tr>"
            for x in comp.get("differences", []) or []
        )
        parts.append(
            f"<h3>{_esc(comp.get('environment_a'))[:12]}… vs {_esc(comp.get('environment_b'))[:12]}…</h3>"
            f"<table><thead><tr><th>Finding</th><th>A</th><th>B</th><th>差异</th><th>满足V1显著规则</th></tr></thead><tbody>{rows}</tbody></table>"
            "<p class='small'>A/B 关联证据不独立确认因果或根因。</p>"
        )
    return "".join(parts)


def _scope_line(card: dict) -> str:
    scope = card.get("scope") or {}
    parts = []
    for label, key in (
        ("层", "layer"),
        ("方向", "direction"),
        ("PCM Tap", "pcm_tap"),
        ("Call", "call_id"),
        ("RTP Stream", "rtp_stream_id"),
        ("SSRC", "ssrc"),
    ):
        if scope.get(key) not in (None, "", "UNKNOWN"):
            parts.append(f"{label}：{_esc(scope.get(key))}")
    return "｜".join(parts) or "范围：UNKNOWN（当前 Evidence 不足以绑定更细 Scope）"


def _time_line(card: dict) -> str:
    value = card.get("time") or {}
    start = value.get("absolute_start_utc")
    end = value.get("absolute_end_utc")
    if start is None and end is None:
        return "绝对时间：UNKNOWN（当前 Evidence 未提供可绑定时间）"
    absolute = f"{_esc(start or 'UNKNOWN')} ～ {_esc(end or start or 'UNKNOWN')}"
    relative = value.get("call_relative_representative") or value.get("call_relative_start")
    semantics = str(value.get("semantics") or "EVENT_OR_ANALYZER_RANGE")
    semantic_cn = {
        "OBSERVATION_BOUNDARY": "观察边界（非精确异常区间）",
        "ANALYSIS_WINDOW": "分析窗口（非精确异常区间）",
        "EVENT_OR_ANALYZER_RANGE": "Finding/Analyzer 时间范围",
        "UNKNOWN": "时间边界未知",
    }.get(semantics, semantics)
    exact = "是" if value.get("exact_event_window_known") else "否"
    return (
        f"绝对时间（UTC）：{absolute}｜时间语义：{_esc(semantic_cn)}｜精确异常首末时刻：{exact}"
        + (f"｜Call 相对时间：{_esc(relative)}" if relative else "")
    )


def _measurements_html(card: dict) -> str:
    rows = []
    for measurement in card.get("measurements") or []:
        unit = f" {_esc(measurement.get('unit'))}" if measurement.get("unit") else ""
        meaning = f"<div class='small'>{_esc(measurement.get('meaning'))}</div>" if measurement.get("meaning") else ""
        rows.append(
            f"<div class='metric'><b>{_esc(measurement.get('label'))}</b><span>{_esc(measurement.get('value'))}{unit}</span>{meaning}</div>"
        )
    return "<div class='metrics'>" + "".join(rows) + "</div>" if rows else "<p class='small'>本 Finding 暂无标准化关键测量项。</p>"


def _packet_refs_html(card: dict) -> str:
    refs = card.get("packet_refs") or []
    if not refs:
        return ""
    rows = "".join(
        f"<tr><td>{_esc(item.get('event'))}</td><td>{_esc(item.get('previous_frame'))} → {_esc(item.get('current_frame'))}</td>"
        f"<td>{_esc(item.get('previous_seq'))} → {_esc(item.get('current_seq'))}</td><td>{_esc(item.get('delta_ms'))}</td>"
        f"<td>{_esc(item.get('classification'))}</td></tr>"
        for item in refs
    )
    return (
        "<div class='evidence-sub'><b>Packet / Frame 下钻</b><table><thead><tr>"
        "<th>#</th><th>Frame</th><th>RTP Seq</th><th>Delta(ms)</th><th>语义</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
    )


def _visuals_html(card: dict) -> str:
    parts = []
    for artifact in card.get("visual_evidence") or []:
        url = _esc(artifact.get("content_url"))
        caption = _esc(artifact.get("caption"))
        atype = _esc(artifact.get("type"))
        if url:
            parts.append(f"<figure><img loading='lazy' src='{url}' alt='{atype}｜{caption}'><figcaption>{atype}｜{caption}</figcaption></figure>")
        else:
            parts.append(f"<figure><figcaption>{atype}｜{caption}｜离线/Bundle 中可查看</figcaption></figure>")
    return (
        "<div class='visual-grid'>" + "".join(parts) + "</div>"
        if parts else
        "<div class='artifact-unavailable'>当前 Finding 尚无精确绑定的可视化 Artifact；不要用其他 Finding 的图片替代。</div>"
    )


def _audio_html(card: dict) -> str:
    audio = card.get("audio_evidence") or {}
    status = audio.get("status")
    if status == "NOT_REQUIRED":
        return ""
    if status != "AVAILABLE":
        return f"<div class='audio-unavailable'><b>异常音频：暂不可用</b><br>{_esc(audio.get('reason'))}</div>"
    clips = []
    for artifact in audio.get("clips") or []:
        url = artifact.get("content_url")
        player = (
            f"<audio controls preload='none' src='{_esc(url)}'>浏览器不支持音频播放。</audio>"
            if url else
            "<div class='small'>离线报告：该音频已进入 Evidence Bundle，可从 Bundle 下载试听。</div>"
        )
        clips.append(
            f"<div class='audio-clip'><div><b>{_esc(artifact.get('type'))}</b>｜{_esc(artifact.get('caption'))}</div>{player}</div>"
        )
    return "<div class='evidence-sub'><b>可试听异常音频</b>" + "".join(clips) + "</div>"


def _action_html(card: dict) -> str:
    contract = card.get("action_contract") or {}
    steps = "".join(f"<li>{_esc(step)}</li>" for step in (contract.get("execution_steps") or []))
    acceptance = card.get("verification_acceptance") or contract.get("acceptance_criteria")
    return (
        f"<div class='next-action'><b>下一步建议：</b>{_esc(card.get('next_action') or '复核本 Finding 的原始 Evidence。')}"
        + (f"<ol>{steps}</ol>" if steps else "")
        + f"<div><b>验证通过标准：</b>{_esc(acceptance or '新增证据必须绑定本 Finding，并使证据边界发生可解释变化。')}</div></div>"
    )


def _evidence_card_html(finding: dict) -> str:
    card = finding.get("evidence_card") or {}
    first = ((finding.get("correlation") or {}).get("first_observable_boundary") or {})
    first_text = first.get("statement") or ("首次可观测层：UNKNOWN（上游证据不足或不可比较）。" if first.get("status") == "UNKNOWN" else "")
    return (
        f"<section class='finding sev-{_esc(finding.get('severity', 'INFO')).lower()}' id='finding-{_esc(finding.get('finding_id') or finding.get('stable_key'))}'>"
        f"<div class='finding-head'><h3>{_esc(finding.get('severity'))}｜{_esc(finding.get('title'))}</h3><span class='evidence-level'>{_esc(finding.get('evidence_level'))}</span></div>"
        f"<p class='scope-line'>{_scope_line(card)}</p><p class='time-line'>{_time_line(card)}</p>"
        f"<div class='statement'><b>发生了什么：</b>{_esc(card.get('what_happened') or finding.get('observation') or 'UNKNOWN（证据不足）')}</div>"
        f"<div class='statement'><b>初步解释：</b>{_esc(card.get('initial_interpretation') or finding.get('interpretation') or '仅记录当前 Evidence 事实。')}</div>"
        + (f"<div class='observed-boundary'><b>{_esc(first_text)}</b></div>" if first_text else "")
        + "<h4>关键测量</h4>" + _measurements_html(card)
        + "<h4>关键可视化证据</h4>" + _visuals_html(card)
        + _audio_html(card)
        + _packet_refs_html(card)
        + f"<div class='boundary'><b>当前不能确认什么：</b>{_esc(card.get('root_cause_boundary') or finding.get('root_cause_boundary') or '具体物理根因尚未确认。')}</div>"
        + _action_html(card)
        + f"<details><summary>研发下钻：原始指标 / Traceability</summary>{_metric_table(finding.get('metrics') or {})}"
          f"<pre>{_esc(json.dumps(card.get('traceability') or {}, ensure_ascii=False, indent=2))}</pre></details></section>"
    )


def _completeness_html(payload: dict) -> tuple[str, str, str]:
    comp = payload.get("completeness") or {}
    frozen = payload.get("capture_quality") or comp.get("frozen_v1") or {}
    dimensions = frozen.get("dimensions") or {}
    if dimensions:
        rows = []
        for name in ("PCAP", "SIP", "RTP", "PCM_RX", "PCM_TX", "DEBUG", "CORRELATION"):
            item = dimensions.get(name) or {}
            available = bool(item.get("available"))
            requirement = "必需" if item.get("requirement") == "REQUIRED" else "可选"
            rows.append(
                f"<tr><td>{_esc(name)}</td><td>{'✅ 可用' if available else '⚠️ 缺失/不可用'}</td><td>{requirement}</td><td>{_esc(item.get('impact'))}</td></tr>"
            )
        table = "<table><thead><tr><th>证据域</th><th>状态</th><th>要求</th><th>结论影响</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        return str(frozen.get("state") or comp.get("state") or "UNKNOWN"), str(frozen.get("boundary") or comp.get("boundary") or ""), table
    capture = comp.get("capture") or {}
    rows = "".join(f"<tr><td>{_esc(key)}</td><td>{'✅ 可用' if value else '⚠️ 缺失/不可用'}</td></tr>" for key, value in capture.items())
    return str(comp.get("state") or "UNKNOWN"), str(comp.get("boundary") or ""), f"<table>{rows}</table>"


def render_report_html(payload: dict) -> str:
    # Final ReportService calls the shared canonical finalizer before this renderer.
    # Rebuilding cards here is idempotent and ensures direct/offline callers see
    # exactly the same Finding projection semantics.
    attach_evidence_cards(payload)
    findings = payload.get("findings", [])
    cards = [_evidence_card_html(finding) for finding in findings]
    if not cards:
        cards = ["<div class='ok'>当前可用证据中未发现明显异常 Finding。</div>"]

    comp = payload.get("completeness") or {}
    completeness_state, completeness_boundary, completeness_table = _completeness_html(payload)
    review = (comp.get("reviewability_contract") or {}).get("state") or comp.get("reviewability") or "UNKNOWN"
    normal = "".join(
        f"<li>✅ {_esc(item.get('text'))}</li>"
        for item in (payload.get("normal_evidence") or payload.get("normal_and_exclusion_evidence") or [])
    ) or "<li>暂无可展示的排除性证据。</li>"
    packet = payload.get("packet_summary") or {}
    stream_rows = "".join(
        f"<tr><td>{_esc(stream.get('source'))}</td><td>{_esc(stream.get('destination'))}</td><td>{_esc(stream.get('packet_count'))}</td>"
        f"<td>{_esc(stream.get('observed_packet_count'))}</td><td>{_esc(stream.get('duplicate_packets'))}</td><td>{_esc(stream.get('lost_packets'))}</td>"
        f"<td>{_esc(stream.get('loss_rate'))}</td><td>{_esc(stream.get('p95_jitter_ms'))}</td><td>{_esc(stream.get('codec'))}</td><td>{_esc(stream.get('ptime_ms'))}</td></tr>"
        for stream in packet.get("streams", [])
    )
    pcm_rows = []
    for stream in (payload.get("pcm_summary") or {}).get("streams", []) or []:
        tap = (stream.get("tap") or {}).get("name")
        for session in stream.get("sessions", []) or []:
            pcm_rows.append(
                f"<tr><td>{_esc(tap)}</td><td>{_esc(session.get('session_index'))}</td><td>{_esc(session.get('packet_count'))}</td>"
                f"<td>{_esc(session.get('rms_dbfs'))}</td><td>{_esc(session.get('peak_dbfs'))}</td>"
                f"<td>{_esc((session.get('hum') or {}).get('dominant_family'))}</td><td>{_esc((session.get('hum') or {}).get('level'))}</td></tr>"
            )
    artifact_list = "".join(
        f"<li>{_esc(artifact.get('type'))}｜{_esc(artifact.get('filename'))}｜SHA256 {_esc(str(artifact.get('sha256') or '')[:12])}…</li>"
        for artifact in payload.get("artifacts", []) or []
    ) or "<li>暂无衍生 Artifact。</li>"

    call = payload.get("display_call") or payload.get("call") or {}
    session = payload.get("session") or {}
    context = payload.get("analysis_context") or {}
    offline = context.get("analysis_mode") == "OFFLINE_IMPORTED"
    if offline:
        call_line = (
            f"<p>分析方式：离线证据导入｜复现 Session：不适用｜重建 Call：{_esc(context.get('reconstructed_call_count'))}。</p>"
            + (
                f"<p>Call：{_esc(call.get('id') or call.get('call_no'))}｜SIP Call-ID：{_esc(call.get('sip_call_id') or call.get('external_call_ref'))}｜"
                f"状态：{_esc(call.get('status'))}｜开始：{_esc(call.get('started_at') or call.get('start_time'))}｜结束：{_esc(call.get('ended_at') or call.get('end_time'))}｜"
                f"号码：{_esc(call.get('caller') or '?')} → {_esc(call.get('dialed_number') or '?')}</p>"
                if call else
                "<p>Call：UNKNOWN（离线 Evidence 无法唯一绑定诊断 Call）。</p>"
            )
        )
        session_history = "当前为离线证据导入；不会为了填充报告而创建 ReproductionSession/ReproductionCall。"
    else:
        call_line = (
            f"<p>Session：{_esc(session.get('id'))}｜Call：{_esc(call.get('call_no') or call.get('id'))}｜状态：{_esc(call.get('status'))}｜"
            f"开始：{_esc(call.get('started_at'))}｜结束：{_esc(call.get('ended_at'))}</p>"
        )
        session_history = "Canonical JSON 保留 scope/environment/version；Case 页面按最新优先展示历史 Session，不同 Environment Fingerprint 不直接混合统计。"

    card_summary = payload.get("evidence_card_summary") or {}
    bundle = payload.get("evidence_bundle_summary") or {}
    bundle_status = "已生成" if bundle.get("status") == "AVAILABLE" else "可通过 Bundle API 生成/下载"
    canonical = payload.get("canonical_finalization") or {}

    return f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>{_esc((payload.get('case') or {}).get('case_no'))} VOIP 初步证据分析报告</title><style>
body{{font-family:Arial,'Microsoft YaHei',sans-serif;max-width:1180px;margin:28px auto;color:#1f2937;line-height:1.65;padding:0 18px}}h1,h2,h3,h4{{color:#111827}}.hero{{padding:18px;border:1px solid #d1d5db;border-radius:12px;background:#f8fafc}}.badge{{display:inline-block;padding:3px 9px;border-radius:10px;background:#e5e7eb;margin-right:6px}}table{{width:100%;border-collapse:collapse;margin:8px 0}}th,td{{border:1px solid #d1d5db;padding:7px;vertical-align:top}}th{{background:#f3f4f6}}.finding{{border:1px solid #d1d5db;border-left:5px solid #6b7280;padding:16px;margin:18px 0;border-radius:10px;background:#fff}}.sev-high,.sev-critical{{border-left-color:#b91c1c}}.sev-medium{{border-left-color:#d97706}}.finding-head{{display:flex;justify-content:space-between;gap:10px;align-items:center}}.finding-head h3{{margin:0}}.evidence-level{{background:#eef2ff;padding:2px 8px;border-radius:10px;font-size:12px}}.scope-line,.time-line{{color:#475569;font-size:14px;margin:5px 0}}.statement{{padding:8px 0}}.boundary{{background:#fff7ed;padding:10px;border-radius:6px;margin-top:12px}}.next-action{{background:#eff6ff;padding:10px;border-radius:6px;margin-top:10px}}.observed-boundary{{background:#f0fdf4;padding:8px;border-radius:6px}}.ok{{background:#ecfdf5;padding:12px;border-radius:8px}}.small{{color:#6b7280;font-size:13px}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px;margin:8px 0 12px}}.metric{{border:1px solid #e5e7eb;border-radius:8px;padding:8px;background:#f8fafc}}.metric b,.metric span{{display:block}}.metric span{{font-size:18px;margin-top:3px}}.visual-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}}figure{{margin:0;border:1px solid #e5e7eb;border-radius:8px;padding:8px;background:#fafafa}}figure img{{width:100%;height:auto;display:block}}figcaption{{font-size:13px;color:#475569;margin-top:6px}}.audio-clip{{border:1px solid #dbeafe;background:#eff6ff;padding:9px;border-radius:8px;margin:8px 0}}audio{{width:100%;margin-top:5px}}.audio-unavailable,.artifact-unavailable{{background:#fffbeb;border:1px solid #fde68a;padding:9px;border-radius:8px;margin:8px 0;color:#92400e}}.evidence-sub{{margin-top:12px}}details{{margin-top:12px}}pre{{white-space:pre-wrap;background:#f8fafc;padding:8px;border-radius:6px;overflow:auto}}@media(max-width:700px){{.visual-grid{{grid-template-columns:1fr}}.finding-head{{display:block}}}}</style></head><body>
<h1>VOIP 初步证据分析报告</h1><div class='hero'><span class='badge'>{_esc((payload.get('case') or {}).get('case_no'))}</span><span class='badge'>{_esc((payload.get('scope') or {}).get('type'))}</span><span class='badge'>Report V{_esc(payload.get('version') or payload.get('report_version'))}</span><h2>{_esc(payload.get('headline'))}</h2><p>{_esc((payload.get('case') or {}).get('summary'))}</p><p class='small'>RTP（Real-time Transport Protocol，实时传输协议）；PCM（Pulse Code Modulation，脉冲编码调制）；dBFS（相对于数字满量程的分贝）。</p></div>
<h2>0. 当前状态 / 快速导航</h2><p>范围：{_esc((payload.get('scope') or {}).get('type'))}｜证据完整度：{_esc(completeness_state)}｜可复核性：{_esc(review)}｜Finding：{_esc(payload.get('finding_count'))}｜最高等级：{_esc(payload.get('highest_severity'))}</p><p class='small'>Evidence Card：{_esc(card_summary.get('finding_count'))}；Scope已绑定：{_esc(card_summary.get('cards_with_bound_scope'))}；时间已绑定：{_esc(card_summary.get('cards_with_bound_time'))}；有通过标准：{_esc(card_summary.get('cards_with_acceptance'))}；异常音频已匹配：{_esc(card_summary.get('audio_available_count'))}。</p>
<h2>1. 当前初步结论</h2><p>{_esc((payload.get('preliminary_assessment') or {}).get('summary'))}</p><div class='boundary'>{_esc((payload.get('evidence_boundary') or {}).get('statement'))}</div>
<h2>2. 当前重点问题</h2>{''.join(cards)}
<h2>3. 证据完整度</h2><p><b>{_esc(completeness_state)}</b>：{_esc(completeness_boundary)}</p>{completeness_table}
<h2>4. 最新一次复现结果</h2>{call_line}
<h2>5. 多次复现汇总</h2>{_multi_call_html(payload)}
<h2>6. A/B 对比</h2>{_ab_html(payload)}
<h2>7. 历次 Reproduction Session（复现会话）</h2><p>{_esc(session_history)}</p>
<h2>8. 正常项 / 排除性证据</h2><ul>{normal}</ul>
<h2>9. 完整技术证据</h2><h3>9.1 SIP / RTP</h3><p>总帧/包数：{_esc(packet.get('packet_count'))}；SIP 消息：{_esc(packet.get('sip_message_count'))}；Call：{_esc(packet.get('call_count'))}；RTP Stream：{_esc(packet.get('rtp_stream_count'))}</p><p class='small'>RTP“有效包数”按唯一 Sequence 计数；“观察包数”是抓包中实际看到的 RTP Datagram 数，包含重复包。重复包不会被计为额外有效媒体，也不会被写成 Packet Loss。</p><table><thead><tr><th>源</th><th>目的</th><th>有效包数</th><th>观察包数</th><th>重复包</th><th>丢包</th><th>丢包率</th><th>P95 抖动(ms)</th><th>Codec</th><th>ptime(ms)</th></tr></thead><tbody>{stream_rows}</tbody></table>
<h3>9.2 PCM / 音频</h3><table><thead><tr><th>Tap</th><th>Session</th><th>包数</th><th>RMS dBFS</th><th>Peak dBFS</th><th>工频族</th><th>等级</th></tr></thead><tbody>{''.join(pcm_rows)}</tbody></table><p>RMS/Peak dBFS 均为数字电平，不等价于真实声压级 dB SPL。</p>
<h2>10. Evidence Bundle / 附件</h2><p>Evidence Bundle：{_esc(bundle_status)}。关键证据优先位于对应 Evidence Card；完整 Bundle 包含报告、原始 PCAP、音频、图片、Analyzer JSON、Manifest 与 SHA256SUMS。</p><ul>{artifact_list}</ul>
<h2>11. 报告版本与审计记录</h2><p>Schema：{_esc(payload.get('schema') or payload.get('schema_version'))}｜Composer：{_esc(payload.get('composer_version'))}｜Canonical Finalization：{_esc(canonical.get('contract_version'))}｜Evidence Card：{_esc(card_summary.get('version'))}｜Generated：{_esc(payload.get('generated_at'))}</p><p class='small'>HTML/Web/飞书均为同一 Canonical Report 的投影视图；投影视图不能修改原始 Evidence 或提升 Root Cause Authority。</p></body></html>"""
