from __future__ import annotations
from collections import Counter
from app.contracts.enums import HypothesisState
from .types import DiagnosisDecision, EvidenceRef, HypothesisProposal, PlanAction
from .triage import triage_summary

class DeterministicDiagnosisReasoner:
    """可解释、可回归的M4基线Reasoner。LLM只能在它之上补充语义，不覆盖确定性事实。"""
    version='0.4.0'

    def reason(self, snapshot:dict) -> DiagnosisDecision:
        hypotheses=[]; plan=[]; known=[]; unknown=[]; excluded=[]
        symptoms=triage_summary((snapshot.get('case') or {}).get('summary',''))
        evidences=snapshot.get('evidences',[]); analyzers=snapshot.get('analyzers',{})
        pcap=[e for e in evidences if e.get('type') in {'PCAP','PCAPNG'} or str(e.get('filename','')).lower().endswith(('.pcap','.pcapng'))]
        field_audio=[e for e in evidences if str(e.get('type','')).startswith('FIELD_AUDIO')]
        decodable_audio=[e for e in field_audio if e.get('type')!='FIELD_AUDIO_RAW' or
                         (e.get('metadata') or {}).get('pcm_format')]
        field_images=[e for e in evidences if e.get('type')=='FIELD_IMAGE' or str((e.get('metadata') or {}).get('message_type','')).lower()=='image']
        media=analyzers.get('media_intelligence')
        packet=analyzers.get('packet_intelligence')
        field_audio_result=analyzers.get('field_audio_intelligence')
        image_result=analyzers.get('image_attachment_intelligence')
        field_alignment=analyzers.get('field_media_alignment')

        if not evidences:
            known.append('当前Case尚无可分析Evidence。')
            if snapshot.get('devices'):
                plan.append(PlanAction('COLLECT_PROFILE','先获取设备基础VOIP状态，建立最小证据集。','L1',True,{'profile_id':'voip_basic'},10))
            else:
                plan.append(PlanAction('REQUEST_USER_EVIDENCE','缺少设备信息和原始证据，无法启动自动诊断。','USER',False,{'need':['device_or_pcap']},10))
            return DiagnosisDecision([],plan,'NEED_MORE_EVIDENCE',{'headline':'证据不足，先建立基础证据集。'},known,unknown,excluded)

        if pcap and not media:
            plan.append(PlanAction('RUN_MEDIA_ANALYSIS','发现PCAP但尚未执行统一SIP/RTP/PCM媒体分析。','L0',True,{'evidence_id':pcap[-1]['id'],'profile_id':'ruijie_aim_diag_v1'},5))
            known.append(f'已发现 {len(pcap)} 份PCAP/PCAPNG。')
        audio_analyzed=set((field_audio_result or {}).get('input_evidence_ids') or [])
        pending_audio=[e for e in decodable_audio if e.get('id') not in audio_analyzed]
        image_analyzed=set((image_result or {}).get('input_evidence_ids') or [])
        pending_images=[e for e in field_images if e.get('id') not in image_analyzed]
        if pending_audio:
            plan.append(PlanAction('RUN_FIELD_AUDIO_ANALYSIS','发现现场录音，执行受限解码和本地波形/频谱分析。','L0',True,{'evidence_id':decodable_audio[-1]['id']},6))
            known.append(f'已收到 {len(decodable_audio)} 份可尝试解码的现场录音。')
        if pending_images:
            plan.append(PlanAction('RUN_IMAGE_METADATA_ANALYSIS','发现现场图片，先校验图片容器和基础元数据。','L0',True,{'evidence_id':field_images[-1]['id']},7))
            known.append(f'已收到 {len(field_images)} 张现场图片。')
        raw_audio=[e for e in field_audio if e.get('type')=='FIELD_AUDIO_RAW' and
                   not (e.get('metadata') or {}).get('pcm_format')]
        if raw_audio and not decodable_audio:
            unknown.append('Raw PCM 缺少采样率、位宽、声道和字节序，系统未猜测其格式。')
            plan.append(PlanAction('REQUEST_USER_EVIDENCE','请补充Raw PCM的采样参数，或转换为WAV/OGG/Opus后重新发送。','USER',False,{'need':['pcm_format_metadata_or_audio_container']},35))

        if field_audio_result and field_audio_result.get('result'):
            ar=field_audio_result['result']; summary=ar.get('summary') or {}; findings=ar.get('findings') or []
            audio_available=summary.get('availability')=='ANALYZED'
            if audio_available:
                known.append(f"现场录音已分析：{summary.get('duration_seconds','?')}秒，{summary.get('sample_rate','?')}Hz，RMS {summary.get('rms_dbfs','?')} dBFS。")
            else:
                unknown.append(f"现场录音内容未解码：{summary.get('reason','当前格式不可用')}。")
            types={x.get('type') for x in findings}
            if 'FIELD_RECORDING_CLIPPED' in types:
                hypotheses.append(self._h('FIELD_RECORDING_CLIPPING','现场录音存在削波候选','Field recording',0.62,'OPEN',False,'FIELD_RECORDING_CLIPPED',field_audio_result['run_id'],'现场录音样本接近满幅的比例异常；可能来自现场声源、录音设备或传输转换，不能直接归因于VoIP系统。'))
            if 'FIELD_RECORDING_CLICK_POP_CANDIDATE' in types:
                hypotheses.append(self._h('FIELD_RECORDING_CLICK_POP','现场录音存在Click/Pop候选','Field recording',0.58,'OPEN',False,'FIELD_RECORDING_CLICK_POP_CANDIDATE',field_audio_result['run_id'],'现场录音检测到多特征Click/Pop候选；尚未与通话PCM/RTP时间轴对齐。'))
            if 'FIELD_RECORDING_NARROWBAND_TONE_CANDIDATE' in types:
                hypotheses.append(self._h('FIELD_RECORDING_NARROWBAND_TONE','现场录音存在窄带音调候选','Field recording',0.60,'OPEN',False,'FIELD_RECORDING_NARROWBAND_TONE_CANDIDATE',field_audio_result['run_id'],'现场录音频谱存在窄带峰值；尚不能区分环境声、录音链路或VoIP通话链路来源。'))
            alignment_availability=(((field_alignment or {}).get('result') or {}).get('summary') or {}).get('availability')
            if alignment_availability!='ALIGNED':
                unknown.append('现场录音尚未与同一通话的SIP/RTP/终端PCM及异常时间点可靠对齐，不能单独定位根因。')
        if image_result and image_result.get('result'):
            ir=image_result['result']; summary=ir.get('summary') or {}
            if summary.get('availability')=='METADATA_ONLY':
                size=f"，{summary.get('width')}×{summary.get('height')}" if summary.get('width') and summary.get('height') else ''
                known.append(f"现场图片文件头有效：{summary.get('format','未知格式')}{size}。")
            else:
                unknown.append(f"现场图片容器未能解析：{summary.get('reason','当前格式不可用')}。")
            ocr=ir.get('ocr') or {}; observations=ocr.get('observations') or []
            if ocr.get('availability')=='EXTRACTED':
                known.append(f"图片OCR提取到 {summary.get('ocr_character_count',0)} 个字符；平均置信度 {ocr.get('mean_confidence','?')}，仅作为L4候选。")
                for item in observations[:4]:
                    known.append(f"截图OCR候选 {item.get('key')}={item.get('value')}（需交叉验证）。")
            else:
                unknown.append('图片未提取到可用文字；当前不会推断拓扑、连线或颜色语义。')
            if not pcap and not field_audio_result:
                reason=('截图文字已提取为候选；请提供原始日志/配置导出用于核对。' if ocr.get('availability')=='EXTRACTED'
                        else '图片未提取到可靠文字；请用文字说明关键告警/配置，或补充原始日志/PCAP。')
                plan.append(PlanAction('REQUEST_USER_EVIDENCE',reason,'USER',False,{'need':['raw_log_or_config_export_or_pcap']},45))
        if field_audio_result and field_audio_result.get('result') and not pcap:
            audio_summary=(field_audio_result['result'].get('summary') or {})
            if audio_summary.get('availability')=='ANALYZED':
                reason='录音特征已提取；请补充异常发生时间点，并尽量提供同一次通话的PCAP，以便进行跨层对齐。'
                need=['anomaly_timestamp_and_matching_pcap']
            else:
                reason='当前录音格式无法可靠解码；请转换为WAV/OGG/Opus后重新发送。'
                need=['supported_audio_recording']
            plan.append(PlanAction('REQUEST_USER_EVIDENCE',reason,'USER',False,{'need':need},46))
        alignment_inputs=set((field_alignment or {}).get('input_evidence_ids') or [])
        alignment_config=(field_alignment or {}).get('config_snapshot') or {}
        alignment_current=bool(decodable_audio and decodable_audio[-1].get('id') in alignment_inputs and alignment_config.get('media_run_id')==media.get('run_id')) if media else False
        if field_audio_result and media and not alignment_current and decodable_audio and not pending_audio:
            audio_summary=(field_audio_result.get('result') or {}).get('summary') or {}
            if audio_summary.get('availability')=='ANALYZED':
                plan.append(PlanAction('RUN_FIELD_MEDIA_ALIGNMENT','现场录音和PCAP媒体均已完成分析，执行确定性信号相关与时间映射。','L0',True,
                                       {'evidence_id':decodable_audio[-1]['id'],'media_run_id':media.get('run_id')},8))
        if field_alignment and field_alignment.get('result'):
            alignment_result=field_alignment['result']; alignment_summary=alignment_result.get('summary') or {}
            if alignment_summary.get('availability')=='ALIGNED':
                best=(alignment_result.get('alignments') or [{}])[0]; corr=best.get('correlation') or {}
                known.append(f"现场录音已与 {best.get('source')} 媒体对齐：相关质量 {corr.get('quality')}，系数 {corr.get('absolute_correlation')}，偏移 {corr.get('lag_ms')}ms。")
                mapped=best.get('mapped_events') or []
                if mapped:
                    known.append(f"已把 {len(mapped)} 个现场录音事件映射到抓包绝对时间/Call。")
            else:
                unknown.append('现场录音与当前PCAP的RTP/PCM未找到可靠信号匹配，不能确认来自同一次通话。')

        result=None
        source_run=None
        if media and media.get('result'):
            result=media['result']; source_run=media['run_id']
        elif packet and packet.get('result'):
            result=packet['result']; source_run=packet['run_id']
        if result:
            self._reason_from_result(result,source_run,hypotheses,known,unknown,excluded,plan,symptoms)
        # A reproduction CALL_QUICK run may have produced real findings
        # (verdict/role/findings) even when a classic media/packet analyzer exists
        # (e.g. a clean media analysis over a real call whose DTMF was only surfaced
        # by the reproduction analyzer). Always feed reproduction findings into the
        # same deterministic mapping so they complement, not get shadowed by, the
        # classic analyzer result.
        repro = analyzers.get('REPRODUCTION_CALL_QUICK_EVIDENCE')
        if repro and repro.get('result'):
            self._reason_from_reproduction(repro, hypotheses, known, plan, symptoms)

        # 设备文本证据存在，但没有网络/媒体证据时，提示上传/采集PCAP。
        if not pcap and not result and not field_audio and not field_images:
            plan.append(PlanAction('REQUEST_USER_EVIDENCE','当前只有设备侧文本证据，SIP/RTP媒体问题仍需PCAP才能定量分析。','USER',False,{'need':['pcap_or_pcapng']},20))
            unknown.append('缺少SIP/RTP媒体抓包。')

        hypotheses=self._dedupe(hypotheses)
        if 'AUDIO_NOISE' in symptoms and result and not any(h.code in {'PCM_HUM_INTERFERENCE','LOCAL_CAPTURE_PERIODIC_INTERFERENCE'} and h.status=='SUPPORTED' for h in hypotheses):
            if not any(a.action_type=='REQUEST_USER_EVIDENCE' for a in plan):
                plan.append(PlanAction('REQUEST_USER_EVIDENCE','用户现象为杂音/电流音，但当前没有与听感时刻直接对齐的音频根因证据；请提供异常发生时间点或现场录音用于频谱/PCM时间关联。','USER',False,{'need':['anomaly_timestamp_or_field_recording']},55))
            unknown.append('当前尚未把PCM频谱/Click/Silence候选与用户实际听到电流音的时间点直接对齐。')
        auto=[x for x in plan if x.auto_execute]
        relevant_supported=[h for h in hypotheses if h.status==HypothesisState.SUPPORTED.value and h.confidence>=0.85]
        if not auto and not plan and not relevant_supported and result:
            plan.append(PlanAction('REQUEST_USER_EVIDENCE','当前确定性证据尚不足以解释用户现象，需要补充复现异常时间点、现场录音或新的抓包证据。','USER',False,{'need':['anomaly_timestamp_or_recording_or_new_capture']},90))
        if relevant_supported:
            # A sufficiently-supported hypothesis (>=0.85) is a deterministic
            # conclusion; do not let pending auto-collection plans downgrade it to
            # NEED_MORE_EVIDENCE and loop until the no-progress guard stalls the run.
            state='DIAGNOSED'
        elif auto: state='NEED_MORE_EVIDENCE'
        else: state='WAITING_USER'
        rank={
            HypothesisState.CONFIRMED.value:6,
            HypothesisState.STRONGLY_SUPPORTED.value:5,
            HypothesisState.SUPPORTED.value:4,
            HypothesisState.OPEN.value:3,
            HypothesisState.CONTRADICTED.value:1,
            HypothesisState.REJECTED.value:0,
        }
        top=sorted(hypotheses,key=lambda h:(rank.get(h.status,0),h.confidence),reverse=True)[:3]
        headline=(top[0].title if top and top[0].status in {HypothesisState.SUPPORTED.value,HypothesisState.STRONGLY_SUPPORTED.value,HypothesisState.CONFIRMED.value} else (f'候选方向，需补证：{top[0].title}' if top and top[0].status==HypothesisState.OPEN.value else ('需要补充证据' if plan else '当前未形成可靠根因假设')))
        summary={
            'headline':headline,
            'top_hypotheses':[{'code':h.code,'title':h.title,'confidence':round(h.confidence,3),'status':h.status} for h in top],
            'auto_action_count':len(auto),'planned_action_count':len(plan),
        }
        return DiagnosisDecision(hypotheses,plan,state,summary,known,unknown,excluded)

    def _reason_from_result(self,result,run_id,hypotheses,known,unknown,excluded,plan,symptoms):
        packet=result.get('packet',result)
        anomalies=packet.get('anomalies',[]) or []
        counts=Counter(a.get('type') for a in anomalies)
        calls=packet.get('calls',[]) or []
        regs=packet.get('registrations',[]) or []
        rtp=packet.get('rtp_streams',[]) or []
        correlations=result.get('correlations',[]) or []
        cross=result.get('cross_layer_events',[]) or []

        if regs:
            ok=sum(1 for r in regs if r.get('status') in {'SUCCESS','REGISTERED'})
            failed=sum(1 for r in regs if r.get('status')=='FAILED')
            known.append(f'SIP注册会话 {len(regs)} 个：成功 {ok}，失败 {failed}。')
        if calls:
            established=sum(1 for c in calls if c.get('state') in {'ESTABLISHED','TERMINATED'})
            known.append(f'SIP呼叫 {len(calls)} 通，其中已建立/正常结束 {established} 通。')
        if rtp:
            known.append(f'识别到 {len(rtp)} 路RTP媒体流。')

        if counts['SIP_REGISTRATION_FAILED']:
            hypotheses.append(self._h('SIP_REGISTRATION_PATH_FAILURE','SIP注册路径异常','SIP/Register',0.94,'SUPPORTED',False,'SIP_REGISTRATION_FAILED',run_id,'检测到确定性SIP注册失败事件；该证据确认“注册失败”，但尚不能单独确认账号、网络或PBX侧具体根因。'))
        if counts['SIP_CALL_FAILED']:
            hypotheses.append(self._h('SIP_CALL_SETUP_FAILURE','SIP呼叫建立异常','SIP/Call',0.93,'SUPPORTED',False,'SIP_CALL_FAILED',run_id,'检测到INVITE事务未成功建立呼叫；该证据确认呼叫建立失败，但不直接等同于具体配置/网络根因。'))
        if counts['ONE_WAY_RTP_MEDIA']:
            hypotheses.append(self._h('ONE_WAY_AUDIO_PATH','SIP已建立但RTP媒体仅单方向存在','RTP/Media',0.95,'SUPPORTED',False,'ONE_WAY_RTP_MEDIA',run_id,'在完整INVITE/2xx/ACK且SDP期望sendrecv的前提下，仅检测到一个方向持续RTP；可确认单向RTP现象，但具体根因仍需结合终端PCM、PBX/多点抓包和NAT/路由状态定位。'))
            unknown.append('尚不能仅凭单点PCAP区分单通由发送端未发RTP、中间网络/NAT丢弃、PBX未转发或接收端媒体处理异常导致。')
            if not self._has_multi_capture(result):
                plan.append(PlanAction('REQUEST_MULTI_POINT_PCAP','单向RTP已确认；增加PBX侧或对端抓包可定位缺失方向在哪一段消失。','USER',False,{'purpose':'locate_one_way_media_segment'},58))
        if counts['CODEC_NEGOTIATION_MISMATCH']:
            hypotheses.append(self._h('CODEC_NEGOTIATION_MISMATCH','SDP协商与实际RTP Codec不一致','DSP/Codec',0.97,'SUPPORTED',True,'CODEC_NEGOTIATION_MISMATCH',run_id,'SDP协商Codec与实际RTP Payload映射存在直接不一致。'))
        burst=counts['BURST_LOSS']; loss=counts['PACKET_LOSS']; delta=counts['HIGH_DELTA']; jitter=counts['HIGH_JITTER']
        if burst or loss:
            conf=min(0.97,0.80+0.03*burst+0.01*loss); status=HypothesisState.SUPPORTED.value
            rationale=f'检测到 RTP PACKET_LOSS={loss}、BURST_LOSS={burst}。'
            confirmable=bool(burst>0 and 'AUDIO_STUTTER' in symptoms)
            if 'AUDIO_NOISE' in symptoms and 'AUDIO_STUTTER' not in symptoms:
                conf*=0.75; status=HypothesisState.OPEN.value; confirmable=False; rationale+=' 用户现象偏向电流音/杂音，丢包更能解释卡顿/断音，不能单独解释稳定音色类异常。'
            hypotheses.append(self._h('RTP_PACKET_LOSS_PATH','RTP媒体链路存在丢包/突发丢包','RTP/Network',conf,status,confirmable,'BURST_LOSS' if burst else 'PACKET_LOSS',run_id,rationale))
            if not self._has_multi_capture(result):
                plan.append(PlanAction('REQUEST_MULTI_POINT_PCAP','当前只能确认RTP丢包现象，单抓包点无法定位具体丢包区间。','USER',False,{'purpose':'locate_loss_segment'},60))
                unknown.append('尚不能确定RTP丢包发生在PBX、交换网络还是终端之间。')
        elif delta or jitter:
            conf=min(0.92,0.70+0.025*delta+0.02*jitter)
            status=HypothesisState.SUPPORTED.value
            rationale=f'无明确Sequence丢包根因时，检测到 HIGH_DELTA={delta}、HIGH_JITTER={jitter}。'
            if 'AUDIO_NOISE' in symptoms and 'AUDIO_STUTTER' not in symptoms:
                conf*=0.76; status=HypothesisState.OPEN.value; rationale+=' 用户描述偏向杂音/电流音，RTP到包抖动更能解释卡顿/断音，不能单独解释稳定音色类异常。'
            hypotheses.append(self._h('RTP_ARRIVAL_JITTER','RTP媒体到包存在瞬时抖动/高Delta','RTP/Network',conf,status,False,'HIGH_DELTA' if delta else 'HIGH_JITTER',run_id,rationale))
            known.append('当前RTP异常以到包间隔/抖动为主，不应自动等同于Sequence丢包。')
            if not self._has_multi_capture(result):
                plan.append(PlanAction('REQUEST_MULTI_POINT_PCAP','若需要定位抖动产生的具体网络区间，需要增加PBX侧/中间链路抓包做同包相关。','USER',False,{'purpose':'locate_jitter_segment'},65))
                unknown.append('尚不能仅凭单点抓包确定瞬时RTP到达抖动产生在哪一段链路。')

        high_corr=[c for c in correlations if ((c.get('details') or {}).get('correlation') or {}).get('quality')=='HIGH']
        if high_corr:
            known.append(f'检测到 {len(high_corr)} 组 PCM↔RTP 高相关媒体映射。')
            for c in high_corr[:3]:
                det=c.get('details',{}); corr=(det.get('correlation') or {})
                tap=det.get('pcm_tap'); direction=det.get('rtp_direction')
                hypotheses.append(HypothesisProposal(
                    code=f'MEDIA_PATH_CORRELATED_{str(tap).upper()}',title=f'{tap} 与对应RTP媒体路径内容高度一致',fault_domain='Media/Correlation',confidence=min(0.95,float(corr.get('absolute_correlation',0))),status=HypothesisState.OPEN.value,rationale=f'相关系数 {corr.get("absolute_correlation")}，lag {corr.get("lag_ms")}ms；用于排除/缩小链路边界，不单独作为硬件根因。',
                    evidence=[EvidenceRef('ANALYZER_RUN',run_id,'L1','CONTEXT',1.0,'PCM与RTP内容高相关',{'pcm_tap':tap,'rtp_direction':direction,'correlation':corr})]
                ))

        dtmf_matches=[e for e in cross if e.get('type')=='DTMF_SIP_DIAL_MATCH']
        if dtmf_matches:
            known.append(f'检测到 {len(dtmf_matches)} 个 PCM DTMF 与 SIP拨号目标一致事件。')
            excluded.append('对应已匹配号码的PCM输入→SIP拨号链路未见丢号证据。')

        dtmf_mismatches=[e for e in cross if e.get('type')=='DTMF_SIP_DIAL_MISMATCH']
        if dtmf_mismatches:
            best=dtmf_mismatches[0]; det=best.get('details') or {}
            hypotheses.append(HypothesisProposal(
                'DTMF_DIGIT_ASSEMBLY_MISMATCH','PCM输入DTMF与随后SIP拨号目标不一致','DTMF/Call-Control',0.94,'SUPPORTED',
                f'PCM RX识别序列 {det.get("pcm_digits")} 与随后SIP目标 {det.get("sip_target")} 不一致；该跨层证据支持DTMF采集/号码组装链路存在丢号或错号，但尚需结合aimd/驱动上报时序定位具体层。',
                False,None,[EvidenceRef('ANALYZER_RUN',run_id,'L2','SUPPORT',0.9,'PCM DTMF与SIP目标跨层不一致',{'event':best})]))
            known.append(f'检测到 {len(dtmf_mismatches)} 个 PCM DTMF 与 SIP拨号目标不一致事件。')
            unknown.append('尚未通过aimd/驱动事件时序确定号码是在SLIC/驱动、aimd缓存还是SIP号码组装阶段丢失。')
            plan.append(PlanAction('COLLECT_PROFILE','需要补采DTMF驱动/aimd时序证据以定位具体丢号层。','L1',True,{'profile_id':'voip_basic','purpose':'dtmf_layer_localization'},55))

        echo_events=[e for e in cross if e.get('type')=='ECHO_PATH_DETECTED']
        if echo_events:
            best=max(echo_events,key=lambda e: float((e.get('details') or {}).get('absolute_correlation',0)))
            det=best.get('details') or {}; corr=float(det.get('absolute_correlation',0)); delay=det.get('delay_ms')
            if 'ECHO' in symptoms:
                status=HypothesisState.SUPPORTED.value if corr>=0.75 else HypothesisState.OPEN.value
                conf=min(0.93,0.62+0.35*corr)
                hypotheses.append(HypothesisProposal('AUDIO_ECHO_PATH','PCM RX/TX之间检测到稳定延迟回声路径','Audio/Echo',conf,status,
                    f'参考播放方向与采集方向检测到延迟约 {delay}ms、相关系数 {corr:.3f} 的延迟副本；支持存在回声路径，但不能单独区分声学耦合、混合电路/SLIC或AEC失效。',False,None,
                    [EvidenceRef('ANALYZER_RUN',run_id,det.get('evidence_level','L2'),'SUPPORT',0.9,'RX/TX延迟相关峰',{'event':best})]))
                unknown.append('若用户现象为回声，还需结合AEC开关/ERL/ERLE及换话机/端口实验定位具体回声来源。')
            else:
                known.append(f'检测到RX/TX延迟相关路径（约{delay}ms，相关系数{corr:.3f}）；当前非回声症状，仅作为媒体路径上下文。')

        scoped_silence=[e for e in cross if e.get('type')=='UNEXPECTED_SILENCE']
        scoped_click=[e for e in cross if e.get('type')=='CLICK_POP']
        if scoped_silence:
            hypotheses.append(HypothesisProposal('PCM_UNEXPECTED_SILENCE','活跃通话媒体窗口内存在异常静音/中断候选','Audio/Media',min(0.9,0.70+0.03*len(scoped_silence)),HypothesisState.SUPPORTED.value if 'AUDIO_STUTTER' in symptoms else HypothesisState.OPEN.value,
                f'在SIP Active Media Window内检测到 {len(scoped_silence)} 段被有效音频上下文包围的≥200ms静音；相比全Session静音统计具有更高诊断价值。',False,None,[EvidenceRef('ANALYZER_RUN',run_id,'L2','SUPPORT',0.8,'Active Media Window内异常静音',{'count':len(scoped_silence)})]))
        if scoped_click:
            hypotheses.append(HypothesisProposal('PCM_CLICK_POP','活跃通话媒体窗口内存在Click/Pop候选','Audio/DSP',min(0.84,0.62+0.02*len(scoped_click)),HypothesisState.OPEN.value,
                f'在SIP Active Media Window内检测到 {len(scoped_click)} 个同时满足波形突变、短时能量抬升和宽带成分的Click/Pop候选。',False,None,[EvidenceRef('ANALYZER_RUN',run_id,'L3','SUPPORT',0.65,'多特征Click/Pop候选',{'count':len(scoped_click)})]))

        periodic_events=[e for e in cross if e.get('type')=='LOCAL_CAPTURE_PERIODIC_INTERFERENCE']
        if periodic_events:
            best=max(periodic_events,key=lambda e: float(((e.get('details') or {}).get('strength') or {}).get('pcm_rx',0)))
            det=best.get('details') or {}; pcm_p=det.get('pcm_rx') or {}; up_p=det.get('upstream_rtp') or {}; down_p=det.get('downstream_rtp') or {}
            pcm_ac=(pcm_p.get('representative') or {}).get('autocorrelation') or {}
            up_ac=(up_p.get('representative') or {}).get('autocorrelation') or {}
            down_ac=(down_p.get('representative') or {}).get('autocorrelation') or {}
            comb=(pcm_p.get('comb') or {})
            rationale=(f'检测到 {len(periodic_events)} 组本地采集周期干扰传播证据：pcm_rx低能量片段20ms自相关约 {pcm_ac.get("20ms")}，'
                       f'上行RTP约 {up_ac.get("20ms")}，反向RTP约 {down_ac.get("20ms")}；'
                       f'pcm_rx奇次50Hz谐波梳状命中 {comb.get("hit_count",0)} 个频点。'
                       '证据强支持周期噪声在本地采集链路已形成并进入上行RTP，但不能单独确认电源/接地、话机/线路、FXS/SLIC或PCM接口中的具体硬件根因。')
            hypotheses.append(HypothesisProposal(
                'LOCAL_CAPTURE_PERIODIC_INTERFERENCE',
                '本地音频采集链路存在稳定周期性干扰并进入上行RTP',
                'Audio/Analog',0.96,'SUPPORTED',rationale,False,None,
                [EvidenceRef('ANALYZER_RUN',run_id,'L1','SUPPORT',1.0,'PCM低能量20ms周期+奇次谐波梳状谱及上行RTP传播',{'event':best})]
            ))
            known.append(f'检测到 {len(periodic_events)} 组“pcm_rx → 上行RTP”稳定周期干扰传播证据。')
            known.append('该周期特征以约20ms重复和150/250/350/...Hz梳状谱为核心，不要求50Hz基波本身占优。')
            excluded.append('PBX/下行网络不是当前持续周期底噪的主要引入点（反向RTP未表现出同等级周期特征）。')
            unknown.append('具体硬件来源仍需在电源/接地、电话机/线路、FXS/SLIC模拟前端与PCM采集接口之间通过A/B实验闭环。')
            plan.append(PlanAction('REQUEST_USER_EVIDENCE','当前已收敛到本地采集链路；下一步需要A/B实验区分具体硬件节点。','USER',False,{'need':['replace_power_supply','replace_phone_or_line','change_fxs_port','compare_another_device'],'purpose':'close_specific_hardware_root_cause'},40))

        # PCM/audio异常：兼容不同版本输出结构。
        pcm=result.get('pcm') or {}
        hum_high=0; click_count=0; silence_long=0
        for stream in pcm.get('streams',[]) or []:
            for sess in stream.get('sessions',[]) or []:
                spectral=sess.get('spectral') or sess.get('spectral_tone') or {}
                hum=sess.get('hum') or {}
                if str(hum.get('level','')).upper()=='HIGH' or str(spectral.get('hum_score','')).upper()=='HIGH': hum_high+=1
                click_count += len(sess.get('click_pop_events',[]) or [])
                silence_long += sum(1 for x in (sess.get('silence_events',[]) or []) if float(x.get('duration_ms',0))>=200)
        if hum_high:
            if 'AUDIO_NOISE' in symptoms:
                hypotheses.append(self._h(
                    'PCM_HUM_INTERFERENCE','PCM音频存在明显工频/谐波干扰',
                    'Audio/Analog',0.92,'SUPPORTED',False,'PCM_HUM',run_id,
                    f'有 {hum_high} 个PCM Session达到HIGH hum score，且用户症状为噪声/电流音。'))
            else:
                known.append(
                    f'有 {hum_high} 个PCM Session达到HIGH hum score；当前未报告噪声/电流音，'
                    '仅保留为频谱候选，不提升为故障假设。')
        if click_count and not scoped_click:
            # 未获得SIP Active Media Window时保留全Session候选，但降低为L3。
            hypotheses.append(HypothesisProposal('PCM_CLICK_POP','PCM音频存在Click/Pop候选','Audio/DSP',min(0.68,0.42+0.004*click_count),HypothesisState.OPEN.value,f'检测到 {click_count} 个Click/Pop候选；未与用户感知时刻对齐，暂不作为确认根因。',False,None,[EvidenceRef('ANALYZER_RUN',run_id,'L3','SUPPORT',0.5,'启发式Click/Pop候选',{'count':click_count})]))
        if silence_long and not scoped_silence:
            hypotheses.append(HypothesisProposal('PCM_UNEXPECTED_SILENCE','PCM音频存在持续静音候选','Audio/Media',min(0.68,0.46+0.006*silence_long),HypothesisState.OPEN.value,f'检测到 {silence_long} 个≥200ms静音候选；需要结合通话阶段和用户异常时间判断是否为真实异常。',False,None,[EvidenceRef('ANALYZER_RUN',run_id,'L3','SUPPORT',0.5,'启发式Silence候选',{'count':silence_long})]))

    def _h(self,code,title,domain,confidence,status,confirmable,event_type,run_id,rationale):
        return HypothesisProposal(code,title,domain,confidence,status,rationale,confirmable,event_type,[EvidenceRef('ANALYZER_RUN',run_id,'L1','SUPPORT',1.0,rationale,{'event_type':event_type})])

    def _reason_from_reproduction(self, repro: dict, hypotheses: list, known: list, plan: list, symptoms: set):
        """Interpret an autonomous-reproduction CALL_QUICK run into hypotheses.

        CALL_QUICK summary carries ``verdict``/``role``/``findings`` produced by the
        deterministic Media/PCM analyzers on the real (or mock) captured call. This
        maps reproduction findings onto the same diagnostic hypotheses the reasoner
        already uses, so reproduction evidence reaches the diagnosis (previously it
        was collected in the snapshot but never reasoned over).
        """
        run_id = repro.get('run_id')
        result = repro.get('result') or {}
        summary = result.get('summary') or {}
        findings = set(summary.get('findings') or [])
        verdict = summary.get('verdict')
        role = summary.get('role')
        media_summary = summary.get('media_summary') or {}
        if not findings and not verdict:
            return
        known.append(
            f'自动复现CALL_QUICK：verdict={verdict} role={role} findings={sorted(findings) if findings else "-"}'
        )
        # Mapping from reproduction finding -> (code,title,domain,event) mirroring the
        # deterministic media/packet analyzer semantics used in _reason_from_result.
        mapping = {
            'SIP_REGISTRATION_FAILED': ('SIP_REGISTRATION_PATH_FAILURE', 'SIP注册路径异常', 'SIP/Register', 'SIP_REGISTRATION_FAILED', 0.94),
            'SIP_CALL_FAILED': ('SIP_CALL_SETUP_FAILURE', 'SIP呼叫建立异常', 'SIP/Call', 'SIP_CALL_FAILED', 0.93),
            'ONE_WAY_RTP_MEDIA': ('ONE_WAY_AUDIO_PATH', 'SIP已建立但RTP媒体仅单方向存在', 'RTP/Media', 'ONE_WAY_RTP_MEDIA', 0.95),
            'RTP_BURST_LOSS': ('RTP_PACKET_LOSS_PATH', 'RTP媒体链路存在丢包/突发丢包', 'RTP/Network', 'BURST_LOSS', 0.97),
            'PACKET_LOSS': ('RTP_PACKET_LOSS_PATH', 'RTP媒体链路存在丢包', 'RTP/Network', 'PACKET_LOSS', 0.90),
            'ECHO_PATH': ('ECHO_PATH_ISSUE', '通话存在回声路径', 'Audio/Echo', 'ECHO_PATH', 0.88),
            'PERIODIC_INTERFERENCE': ('LOCAL_CAPTURE_PERIODIC_INTERFERENCE', '本地采集存在周期干扰（电流音特征）', 'Audio/Analog', 'PERIODIC_INTERFERENCE', 0.90),
            'DTMF_PATH': ('DTMF_DIGIT_ASSEMBLY_MISMATCH', 'DTMF拨号/收号链路异常', 'DSP/DTMF', 'DTMF_PATH', 0.85),
            'CODEC_NEGOTIATION_MISMATCH': ('CODEC_NEGOTIATION_MISMATCH', 'SDP协商与实际RTP Codec不一致', 'DSP/Codec', 'CODEC_NEGOTIATION_MISMATCH', 0.97),
        }
        for finding, (code, title, domain, event, base_conf) in mapping.items():
            if finding not in findings:
                continue
            # These findings establish path observability, not a defect by
            # themselves.  Only promote them when the reported symptom asks the
            # corresponding diagnostic question; mismatch/anomaly events are
            # handled independently by the full analyzer above.
            if finding == 'ECHO_PATH' and 'ECHO' not in symptoms:
                known.append('自动复现观察到RX/TX延迟相关路径；当前非回声症状，不将路径存在解释为回声故障。')
                continue
            if finding == 'DTMF_PATH' and 'DTMF_LOSS' not in symptoms:
                known.append('自动复现识别到DTMF序列；仅证明拨号路径可观测，不代表丢号或错号。')
                continue
            conf = base_conf
            if finding == 'PERIODIC_INTERFERENCE' and 'AUDIO_NOISE' not in symptoms:
                conf = 0.82
            if finding == 'RTP_BURST_LOSS' and 'AUDIO_STUTTER' not in symptoms:
                conf = 0.82
            hypotheses.append(self._h(code, title, domain, conf, 'SUPPORTED', False, event, run_id,
                                      f'自动复现检出 finding={finding}（verdict={verdict} role={role}）。'))
        # Any active media window + call classification strengthen "symptom reproduced".
        if 'ACTIVE_MEDIA_WINDOW' in findings and 'CALL_CLASSIFICATION' in findings:
            known.append('自动复现捕获到活跃媒体窗口并完成呼叫分类（现象可在受控复现中稳定观察到）。')
        mapped_codes = {m[0] for m in mapping.values()}
        if verdict == 'MATCH' and not any(h.code in mapped_codes for h in hypotheses):
            known.append('自动复现判定为TARGET（现象复现），但未检出可归因的确定性异常，需进一步证据。')
            ev_ids = repro.get('input_evidence_ids') or []
            if ev_ids:
                plan.append(PlanAction('RUN_MEDIA_ANALYSIS', '自动复现已复现现象但未定位异常；对复现PCAP执行统一媒体分析以定位根因。', 'L0', True,
                                       {'evidence_id': ev_ids[0], 'profile_id': 'ruijie_aim_diag_v1'}, 30))

    @staticmethod
    def _dedupe(items):
        best={}
        for h in items:
            old=best.get(h.code)
            if not old or h.confidence>old.confidence: best[h.code]=h
        return list(best.values())

    @staticmethod
    def _has_multi_capture(result):
        source=(result.get('packet') or result).get('source',{}) if isinstance(result,dict) else {}
        return bool(source.get('capture_points') and len(source.get('capture_points'))>1)
