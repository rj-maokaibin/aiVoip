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
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def environment_fingerprint(environment:dict|None)->str|None:
    return canonical_hash(environment) if environment else None


def build_completeness(*,evidences:list[dict],analyzer_states:dict[str,dict],scope_type:str,results:dict[str,dict|None]|None=None)->dict:
    results=results or {}
    def state(name:str)->dict:
        item=analyzer_states.get(name) or {}; status=str(item.get("status") or "UNAVAILABLE")
        return {"status":status,"available":status in {"SUCCESS","PARTIAL_SUCCESS"},"partial":status=="PARTIAL_SUCCESS",
                "reason":item.get("error_code") or item.get("error_message") or item.get("degraded_reason")}
    evidence_types={str(x.get("type") or "").upper() for x in evidences}
    packet=state("packet_intelligence"); pcm_state=state("pcm_intelligence"); media=state("media_intelligence")
    pcm_result=results.get("pcm_intelligence") or {}
    taps={str((x.get("tap") or {}).get("name") or "").lower() for x in pcm_result.get("streams",[]) or []}
    capture={
        "pcap":any(x in evidence_types for x in {"PCAP","PCAPNG"}),
        "pcm_rx":("pcm_rx" in taps) or any("PCM_RX" in x for x in evidence_types),
        "pcm_tx":("pcm_tx" in taps) or any("PCM_TX" in x for x in evidence_types),
        "debug":any("DEBUG" in x or "LOG" in x for x in evidence_types),
    }
    missing=[name for name in ("pcap","pcm_rx","pcm_tx") if not capture[name]]
    analyzers={"packet":packet,"pcm":pcm_state,"media":media}
    unavailable=[name for name,item in analyzers.items() if not item["available"]]
    state_value="COMPLETE" if not missing and not unavailable else "PARTIAL"
    return {"state":state_value,"scope_type":scope_type,"capture":capture,"analyzers":analyzers,
            "missing_required_evidence":missing,"unavailable_analyzers":unavailable,
            "boundary":"证据完整，可进行当前范围的 Packet/PCM/Media 初步证据分析。" if state_value=="COMPLETE" else
                       "部分证据或 Analyzer 不可用；报告仍可生成，但缺失方向不得用于排除或根因确认。"}


def build_packet_summary(packet:dict|None)->dict:
    if not packet:return {"available":False}
    summary=packet.get("summary") or {}; streams=[]
    for stream in packet.get("rtp_streams",[]) or []:
        streams.append({"stream_id":stream.get("stream_id"),"source":f"{stream.get('src_ip')}:{stream.get('src_port')}",
                        "destination":f"{stream.get('dst_ip')}:{stream.get('dst_port')}","ssrc":stream.get("ssrc"),
                        "packet_count":stream.get("packet_count"),"lost_packets":stream.get("lost_packets",stream.get("lost")),
                        "loss_rate":stream.get("loss_rate"),"avg_jitter_ms":stream.get("avg_rfc3550_jitter_ms"),
                        "p95_jitter_ms":stream.get("p95_rfc3550_jitter_ms"),"max_jitter_ms":stream.get("max_rfc3550_jitter_ms"),
                        "max_delta_ms":stream.get("max_delta_ms"),"codec":stream.get("codec"),"ptime_ms":stream.get("ptime_ms")})
    return {"available":True,"packet_count":summary.get("packet_count"),"sip_message_count":summary.get("sip_message_count"),
            "call_count":summary.get("call_count"),"rtp_stream_count":summary.get("rtp_stream_count"),"rtcp_report_count":summary.get("rtcp_report_count"),
            "streams":streams,"calls":packet.get("calls",[])}


def build_pcm_summary(pcm:dict|None)->dict:
    if not pcm:return {"available":False}
    streams=[]
    for stream in pcm.get("streams",[]) or []:
        tap=stream.get("tap") or {}; sessions=[]
        for session in stream.get("sessions",[]) or []:
            signal=session.get("signal") or {}
            sessions.append({"session_index":session.get("session_index"),"start_time":session.get("start_time"),"end_time":session.get("end_time"),
                             "packet_count":session.get("packet_count"),"audio_duration_seconds":session.get("audio_duration_seconds"),
                             "gap_event_count":session.get("gap_event_count"),"rms_dbfs":signal.get("rms_dbfs",signal.get("dbfs")),
                             "peak_dbfs":signal.get("peak_dbfs"),"peak_amplitude":signal.get("peak"),"dc_offset":signal.get("dc_offset"),
                             "clipping_percent":signal.get("clipping_percent"),"hum":session.get("hum"),"spectral":session.get("spectral"),
                             "silence_event_count":len(session.get("silence_events",[]) or []),"click_pop_event_count":len(session.get("click_pop_events",[]) or []),
                             "dtmf_sequences":session.get("dtmf_sequences",[])})
        streams.append({"tap":tap,"packet_count":stream.get("packet_count"),"sessions":sessions})
    return {"available":True,"summary":pcm.get("summary") or {},"format":pcm.get("format") or {},
            "level_definition":{"unit":"dBFS","rms":"RMS dBFS：数字 PCM 音频均方根电平，相对于数字满量程。",
                                "peak":"Peak dBFS：数字 PCM 音频峰值电平，相对于数字满量程。",
                                "boundary":"dBFS 不等于实际声压级 dB SPL；未校准的数字 PCM 不能声称真实声压分贝。"},"streams":streams}


def build_report_payload(*,case:dict,scope_type:str,scope_id:str,session:dict|None,call:dict|None,environment:dict|None,evidences:list[dict],
                         analyzer_states:dict[str,dict],results:dict[str,dict|None],report_version:int,generated_at:str|None=None,
                         analysis_context:dict|None=None,display_call:dict|None=None)->dict:
    packet=results.get("packet_intelligence"); pcm=results.get("pcm_intelligence"); media=results.get("media_intelligence")
    source_run_ids={name:s.get("run_id") for name,s in analyzer_states.items() if s.get("run_id")}
    findings=compose_findings(packet=packet,pcm=pcm,media=media,source_run_ids=source_run_ids)
    completeness=build_completeness(evidences=evidences,analyzer_states=analyzer_states,scope_type=scope_type,results=results)
    resolved_call=display_call if display_call is not None else call
    context=analysis_context or {}
    semantic_issues=list(context.get("semantic_issues") or [])
    if semantic_issues:
        completeness["semantic_status"]="INCOMPLETE"
        completeness["semantic_issues"]=semantic_issues
        completeness["reviewability"]="NOT_FULLY_REVIEWABLE"
        completeness["state"]="PARTIAL"
        prior=str(completeness.get("boundary") or "")
        completeness["boundary"]=("Call/媒体上下文存在语义绑定缺口；报告已降级为 PARTIAL_COMPLETE / NOT_FULLY_REVIEWABLE。 "+prior).strip()
    else:
        completeness["semantic_status"]="OK"
        completeness["semantic_issues"]=[]
        completeness["reviewability"]=context.get("reviewability") or "FULLY_REVIEWABLE"
    normal=build_normal_evidence(packet,pcm,media); highest=findings[0]["severity"] if findings else "INFO"
    headline=f"发现 {len(findings)} 个初步证据问题点，最高等级 {highest}。" if findings else "当前已完成的 Analyzer 未发现明显异常；该结论仅覆盖已采集且可分析的证据范围。"
    boundary={"root_cause_authority":"PRELIMINARY_EVIDENCE_ONLY","statement":"本报告描述已观测事实、候选异常和证据边界，不确认最终 Root Cause（根因）。",
              "historical_case_authority":"历史 Case 和 AI 解释不能将当前 Finding 提升为 L1/L2 或独立确认根因。"}
    payload={"schema_version":REPORT_SCHEMA_VERSION,"composer_version":REPORT_COMPOSER_VERSION,"report_version":report_version,
             "generated_at":generated_at or utcnow_iso(),"scope":{"type":scope_type,"id":scope_id},"case":case,"session":session,"call":resolved_call,
             "display_call":resolved_call,"analysis_context":context,
             "environment":environment or {},"environment_fingerprint":environment_fingerprint(environment),"headline":headline,
             "finding_count":len(findings),"highest_severity":highest,"completeness":completeness,"packet_summary":build_packet_summary(packet),
             "pcm_summary":build_pcm_summary(pcm),"media_summary":(media or {}).get("summary") if media else None,"findings":findings,
             "normal_and_exclusion_evidence":normal,"analyzers":analyzer_states,"artifacts":[],
             "preliminary_assessment":{"summary":headline,"evidence_boundary":boundary["statement"],
                 "recommended_next_action":"优先复核 HIGH/CRITICAL Finding 对应的时间窗、图像和音频；如需确认物理根因，进入确定性 Diagnosis/A-B/Fix Verification 流程。"},
             "evidence_boundary":boundary}
    payload["input_snapshot_hash"]=canonical_hash({"scope":payload["scope"],"case":case,"session":session,"display_call":resolved_call,
        "analysis_context":context,"environment":environment,"evidences":evidences,"analyzers":analyzer_states,
        "result_hashes":{k:canonical_hash(v) if v is not None else None for k,v in results.items()}})
    return payload


def _esc(v:Any)->str:return html.escape(str(v if v is not None else ""))


def _metric_table(metrics:dict)->str:
    if not metrics:return "<p>无附加指标。</p>"
    rows=[]
    for key,value in list(metrics.items())[:18]:
        if isinstance(value,(dict,list)):value=json.dumps(value,ensure_ascii=False,separators=(",",":"))
        rows.append(f"<tr><td>{_esc(key)}</td><td>{_esc(value)}</td></tr>")
    return "<table><tbody>"+"".join(rows)+"</tbody></table>"


def _multi_call_html(payload:dict)->str:
    context=payload.get("analysis_context") or {}; offline=context.get("analysis_mode")=="OFFLINE_IMPORTED"
    summary=payload.get("multi_call_summary") or {}; groups=summary.get("finding_groups") or []
    offline_note=(f"<p>当前为离线证据分析；PCAP 重建 Call 数：{_esc(context.get('reconstructed_call_count'))}。"
                  "此处的“有效 Call 报告数/复现率”仅用于 Reproduction Call，不把离线重建 Call 伪计为复现次数。</p>") if offline else ""
    if not summary:return offline_note or "<p>当前为 Call 级报告，无跨 Call 聚合。</p>"
    rows="".join(f"<tr><td>{_esc(x.get('title'))}</td><td>{_esc(x.get('occurrence_calls'))}/{_esc(x.get('total_calls'))}</td><td>{_esc(round((x.get('reproduction_rate') or 0)*100,2))}%</td><td>{_esc(x.get('stability'))}</td></tr>" for x in groups)
    return offline_note+f"<p>有效 Reproduction Call 报告数：{_esc(summary.get('call_count'))}</p><table><thead><tr><th>问题</th><th>出现</th><th>复现率</th><th>稳定性</th></tr></thead><tbody>{rows}</tbody></table>"


def _ab_html(payload:dict)->str:
    comparisons=payload.get("ab_comparison") or []
    if not comparisons:return "<p>当前没有满足分组条件的 A/B 环境对比。</p>"
    parts=[]
    for comp in comparisons:
        rows="".join(f"<tr><td>{_esc(x.get('title'))}</td><td>{_esc(round((x.get('environment_a_rate') or 0)*100,2))}%</td><td>{_esc(round((x.get('environment_b_rate') or 0)*100,2))}%</td><td>{_esc(round((x.get('absolute_rate_delta') or 0)*100,2))}%</td><td>{'是' if x.get('significant_by_v1_rule') else '否'}</td></tr>" for x in comp.get("differences",[]) or [])
        parts.append(f"<h3>{_esc(comp.get('environment_a'))[:12]}… vs {_esc(comp.get('environment_b'))[:12]}…</h3><table><thead><tr><th>Finding</th><th>A</th><th>B</th><th>差异</th><th>满足V1显著规则</th></tr></thead><tbody>{rows}</tbody></table><p class='small'>A/B 关联证据不独立确认因果或根因。</p>")
    return "".join(parts)


def render_report_html(payload:dict)->str:
    findings=payload.get("findings",[]); cards=[]
    for f in findings:
        tr=f.get("time_range") or {}; first=((f.get("correlation") or {}).get("first_observable_boundary") or {})
        first_text=first.get("statement") or ("首次可观测层：UNKNOWN（未知）。" if first.get("status")=="UNKNOWN" else "")
        cards.append(f"<section class='finding sev-{_esc(f.get('severity','INFO')).lower()}'><h3>{_esc(f.get('severity'))}｜{_esc(f.get('title'))}</h3>"
                     f"<p><b>时间：</b>{_esc(tr.get('start'))} ～ {_esc(tr.get('end'))}　<b>证据等级：</b>{_esc(f.get('evidence_level'))}</p>"
                     f"<p><b>已观测事实：</b>{_esc(f.get('observation'))}</p><p><b>初步解释：</b>{_esc(f.get('interpretation'))}</p>"
                     f"<p><b>{_esc(first_text)}</b></p><div class='boundary'><b>根因边界：</b>{_esc(f.get('root_cause_boundary'))}</div>"
                     f"<details><summary>关键指标 / 研发下钻</summary>{_metric_table(f.get('metrics') or {})}</details></section>")
    if not cards:cards=["<div class='ok'>当前可用证据中未发现明显异常 Finding。</div>"]
    comp=payload.get("completeness") or {}; cap=comp.get("capture") or {}
    capture_rows="".join(f"<tr><td>{_esc(k)}</td><td>{'✅ 可用' if v else '⚠️ 缺失/不可用'}</td></tr>" for k,v in cap.items())
    normal="".join(f"<li>✅ {_esc(x.get('text'))}</li>" for x in payload.get("normal_and_exclusion_evidence",[])) or "<li>暂无可展示的排除性证据。</li>"
    packet=payload.get("packet_summary") or {}
    stream_rows="".join(f"<tr><td>{_esc(s.get('source'))}</td><td>{_esc(s.get('destination'))}</td><td>{_esc(s.get('packet_count'))}</td><td>{_esc(s.get('lost_packets'))}</td><td>{_esc(s.get('loss_rate'))}</td><td>{_esc(s.get('p95_jitter_ms'))}</td><td>{_esc(s.get('codec'))}</td><td>{_esc(s.get('ptime_ms'))}</td></tr>" for s in packet.get("streams",[]))
    pcm_rows=[]
    for stream in (payload.get("pcm_summary") or {}).get("streams",[]) or []:
        tap=(stream.get("tap") or {}).get("name")
        for s in stream.get("sessions",[]) or []:
            pcm_rows.append(f"<tr><td>{_esc(tap)}</td><td>{_esc(s.get('session_index'))}</td><td>{_esc(s.get('packet_count'))}</td><td>{_esc(s.get('rms_dbfs'))}</td><td>{_esc(s.get('peak_dbfs'))}</td><td>{_esc((s.get('hum') or {}).get('dominant_family'))}</td><td>{_esc((s.get('hum') or {}).get('level'))}</td></tr>")
    artifact_list="".join(f"<li>{_esc(a.get('type'))}｜{_esc(a.get('filename'))}｜SHA256 {_esc(a.get('sha256'))[:12]}…</li>" for a in payload.get("artifacts",[]) or []) or "<li>暂无衍生 Artifact。</li>"
    call=payload.get("display_call") or payload.get("call") or {}; session=payload.get("session") or {}; context=payload.get("analysis_context") or {}
    offline=context.get("analysis_mode")=="OFFLINE_IMPORTED"
    if offline:
        section4_title="4. 当前离线 Call 重建结果"
        if call:
            call_line=(f"<p>分析方式：离线证据导入｜复现 Session：不适用｜重建 Call：{_esc(context.get('reconstructed_call_count'))}</p>"
                       f"<p>Call：{_esc(call.get('id') or call.get('call_no'))}｜SIP Call-ID：{_esc(call.get('sip_call_id') or call.get('external_call_ref'))}｜状态：{_esc(call.get('status'))}｜开始：{_esc(call.get('started_at'))}｜结束：{_esc(call.get('ended_at'))}｜号码：{_esc(call.get('caller') or '?')} → {_esc(call.get('dialed_number') or '?')}</p>")
        else:
            call_line=(f"<p>分析方式：离线证据导入｜复现 Session：不适用｜重建 Call：{_esc(context.get('reconstructed_call_count'))}</p>"
                       f"<p>Call：未绑定｜Call Scope：{_esc(context.get('call_scope'))}｜Call Origin：{_esc(context.get('call_origin'))}</p>")
        session_history="当前为离线证据导入，本项不适用；系统不会为了填充报告而创建 ReproductionSession/ReproductionCall。"
    else:
        section4_title="4. 最新一次复现结果"
        call_line=f"<p>Session：{_esc(session.get('id'))}｜Call：{_esc(call.get('call_no'))}｜状态：{_esc(call.get('status'))}｜开始：{_esc(call.get('started_at'))}｜结束：{_esc(call.get('ended_at'))}</p>"
        session_history="本 Canonical JSON 保留 scope/environment/version，可由 Case 页面按最新优先展开历史 Session；不同 Environment Fingerprint 不直接混合统计。"
    return f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>{_esc((payload.get('case') or {}).get('case_no'))} VOIP 初步证据分析报告</title><style>
body{{font-family:Arial,'Microsoft YaHei',sans-serif;max-width:1180px;margin:28px auto;color:#1f2937;line-height:1.65;padding:0 18px}}h1,h2,h3{{color:#111827}}.hero{{padding:18px;border:1px solid #d1d5db;border-radius:12px;background:#f8fafc}}.badge{{display:inline-block;padding:3px 9px;border-radius:10px;background:#e5e7eb;margin-right:6px}}table{{width:100%;border-collapse:collapse;margin:8px 0}}th,td{{border:1px solid #d1d5db;padding:7px;vertical-align:top}}th{{background:#f3f4f6}}.finding{{border:1px solid #d1d5db;border-left:5px solid #6b7280;padding:14px;margin:14px 0;border-radius:8px}}.sev-high,.sev-critical{{border-left-color:#b91c1c}}.sev-medium{{border-left-color:#d97706}}.boundary{{background:#fff7ed;padding:10px;border-radius:6px}}.ok{{background:#ecfdf5;padding:12px;border-radius:8px}}.small{{color:#6b7280;font-size:13px}}</style></head><body>
<h1>VOIP 初步证据分析报告</h1><div class='hero'><span class='badge'>{_esc((payload.get('case') or {}).get('case_no'))}</span><span class='badge'>{_esc(payload.get('scope',{}).get('type'))}</span><span class='badge'>Report V{_esc(payload.get('report_version'))}</span><h2>{_esc(payload.get('headline'))}</h2><p>{_esc((payload.get('case') or {}).get('summary'))}</p><p class='small'>RTP（Real-time Transport Protocol，实时传输协议）；PCM（Pulse Code Modulation，脉冲编码调制）；dBFS（Decibels relative to Full Scale，相对于数字满量程的分贝）。</p></div>
<h2>0. 当前状态 / 快速导航</h2><p>范围：{_esc(payload.get('scope',{}).get('type'))}｜证据完整度：{_esc(comp.get('state'))}｜可复核性：{_esc(comp.get('reviewability'))}｜Finding：{_esc(payload.get('finding_count'))}｜最高等级：{_esc(payload.get('highest_severity'))}</p>
<h2>1. 当前初步结论</h2><p>{_esc((payload.get('preliminary_assessment') or {}).get('summary'))}</p><div class='boundary'>{_esc((payload.get('evidence_boundary') or {}).get('statement'))}</div>
<h2>2. 当前重点问题</h2>{''.join(cards)}
<h2>3. 证据完整度</h2><p><b>{_esc(comp.get('state'))}</b>：{_esc(comp.get('boundary'))}</p><table>{capture_rows}</table>
<h2>{section4_title}</h2>{call_line}
<h2>5. 多次复现汇总</h2>{_multi_call_html(payload)}
<h2>6. A/B 对比</h2>{_ab_html(payload)}
<h2>7. 历次 Reproduction Session（复现会话）</h2><p>{_esc(session_history)}</p>
<h2>8. 正常项 / 排除性证据</h2><ul>{normal}</ul>
<h2>9. 完整技术证据</h2><h3>9.1 SIP / RTP</h3><p>总帧/包数：{_esc(packet.get('packet_count'))}；SIP 消息：{_esc(packet.get('sip_message_count'))}；Call：{_esc(packet.get('call_count'))}；RTP Stream：{_esc(packet.get('rtp_stream_count'))}</p><table><thead><tr><th>源</th><th>目的</th><th>包数</th><th>丢包</th><th>丢包率</th><th>P95 抖动(ms)</th><th>Codec</th><th>ptime(ms)</th></tr></thead><tbody>{stream_rows}</tbody></table>
<h3>9.2 PCM / 音频</h3><table><thead><tr><th>Tap</th><th>Session</th><th>包数</th><th>RMS dBFS</th><th>Peak dBFS</th><th>工频族</th><th>等级</th></tr></thead><tbody>{''.join(pcm_rows)}</tbody></table><p>RMS/Peak dBFS 均为数字电平，不等价于真实声压级 dB SPL。</p>
<h2>10. Evidence Bundle / 附件</h2><ul>{artifact_list}</ul><p>完整原始证据包由 Bundle API 生成，包含 Manifest 与 SHA256SUMS。</p>
<h2>11. 报告版本与审计记录</h2><p>Schema：{_esc(payload.get('schema_version'))}｜Composer：{_esc(payload.get('composer_version'))}｜Generated：{_esc(payload.get('generated_at'))}</p></body></html>"""
