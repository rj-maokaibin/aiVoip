from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
import numpy as np

from app.analyzers.audio.candidate_decision import audio_window_metrics, decide_click_pop, decide_silence
from app.analyzers.audio.features import waveform_envelope, spectrogram_data, spectral_tone_analysis
from app.analyzers.audio.quality import detect_unexpected_silence, detect_click_pop_robust, analyze_echo_path
from app.analyzers.audio.rtp_audio import render_rtp_tracks, RenderedRtpTrack
from app.analyzers.media.correlation import correlate_tracks
from app.analyzers.media.timeline import build_unified_timeline
from app.analyzers.media.periodic import build_periodic_path_analysis
from app.analyzers.audio.periodic import slice_by_absolute_time
from app.analyzers.profile import get_default_analyzer_profile
from app.analyzers.packet import PacketIntelligenceEngine, TSharkAdapter
from app.analyzers.packet.pcap_rtp_fallback import read_rtp_packets_fallback
from app.analyzers.pcm.engine import PcmIntelligenceEngine
from app.analyzers.pcm.profile import PcmProfile
from app.analyzers.pcm.pcap_udp import iter_udp_datagrams
from app.analyzers.pcm.wav import write_wav


class MediaIntelligenceEngine:
    analyzer_name = 'media_intelligence'
    analyzer_version = '0.5.0'

    def __init__(self, pcm_profile: PcmProfile, tshark: TSharkAdapter | None = None):
        self.pcm_profile = pcm_profile
        self.analyzer_profile = get_default_analyzer_profile()
        self.packet_engine = PacketIntelligenceEngine(tshark)

    def analyze_pcap(self, path: str | Path, output_dir: str | Path) -> dict:
        output_dir = Path(output_dir); output_dir.mkdir(parents=True, exist_ok=True)
        degraded_reason = None
        try:
            packets = list(self.packet_engine.tshark.iter_packets(path))
            packet_result = self.packet_engine.analyze_packets(packets)
            if not packet_result.get('rtp_streams'):
                excluded = {tap.dst_port for tap in self.pcm_profile.taps}
                fallback_rtp = read_rtp_packets_fallback(path, exclude_ports=excluded)
                if fallback_rtp:
                    degraded_reason = 'TSHARK_RTP_NOT_DECODED: supplemented by restricted RTP fallback'
                    packets = sorted(packets + fallback_rtp, key=lambda p: (p.timestamp, p.frame_number))
                    packet_result = self.packet_engine.analyze_packets(packets)
                    packet_result['status'] = 'PARTIAL_SUCCESS'
                    packet_result['source'] = {
                        'type':'pcap', 'parser':'tshark+restricted_rtp_fallback',
                        'tshark_version':self.packet_engine.tshark.version(),
                        'degraded_reason':degraded_reason,
                    }
                    packet_result['availability'] = {'sip':'AVAILABLE','sdp':'AVAILABLE','rtp':'AVAILABLE','rtcp':'AVAILABLE'}
                else:
                    packet_result['source'] = {'type':'pcap','parser':'tshark','tshark_version':self.packet_engine.tshark.version()}
                    packet_result['availability'] = {'sip':'AVAILABLE','sdp':'AVAILABLE','rtp':'AVAILABLE','rtcp':'AVAILABLE'}
            else:
                packet_result['source'] = {'type':'pcap','parser':'tshark','tshark_version':self.packet_engine.tshark.version()}
                packet_result['availability'] = {'sip':'AVAILABLE','sdp':'AVAILABLE','rtp':'AVAILABLE','rtcp':'AVAILABLE'}
        except Exception as exc:
            degraded_reason = f'{type(exc).__name__}: {exc}'
            excluded = {tap.dst_port for tap in self.pcm_profile.taps}
            packets = read_rtp_packets_fallback(path, exclude_ports=excluded)
            packet_result = self.packet_engine.analyze_packets(packets)
            packet_result['status'] = 'PARTIAL_SUCCESS'
            packet_result['source'] = {'type':'pcap','parser':'restricted_rtp_fallback','degraded_reason':degraded_reason}
            packet_result['availability'] = {'sip':'UNAVAILABLE','sdp':'UNAVAILABLE','rtp':'AVAILABLE','rtcp':'UNAVAILABLE'}
        pcm_engine = PcmIntelligenceEngine(self.pcm_profile)
        pcm_result = pcm_engine.analyze_pcap(path)
        tracks = render_rtp_tracks(packets, packet_result.get('rtp_streams', []))
        artifacts = []
        media_events = []
        track_results = []
        for idx, track in enumerate(tracks):
            base = f'rtp_{idx:02d}'
            wav = output_dir / f'{base}.wav'; write_wav(wav, track.samples, track.sample_rate, track.channels)
            wave_json = output_dir / f'{base}_waveform.json'; wave_json.write_text(json.dumps(waveform_envelope(track.samples, track.sample_rate), ensure_ascii=False, separators=(',',':')))
            spec_json = output_dir / f'{base}_spectrogram.json'; spec_json.write_text(json.dumps(spectrogram_data(track.samples, track.sample_rate), ensure_ascii=False, separators=(',',':')))
            artifacts.extend([
                _artifact(wav, 'AUDIO_WAV', 'audio/wav', {'stream_id':track.stream_id,'kind':'raw_decoded'}),
                _artifact(wave_json, 'WAVEFORM_JSON', 'application/json', {'stream_id':track.stream_id}),
                _artifact(spec_json, 'SPECTROGRAM_JSON', 'application/json', {'stream_id':track.stream_id}),
            ])
            silence = detect_unexpected_silence(track.samples, track.sample_rate)
            clicks = detect_click_pop_robust(track.samples, track.sample_rate)
            tones = spectral_tone_analysis(track.samples, track.sample_rate)
            rtp_events = next((s.get('events',[]) for s in packet_result.get('rtp_streams',[]) if s.get('stream_id')==track.stream_id), [])
            clips = self._write_event_clips(track, rtp_events, output_dir, base)
            artifacts.extend(clips['artifacts']); media_events.extend(clips['events'])
            meta = track.metadata(); meta.update({'silence_events':silence,'click_pop_events':clicks,'spectral':tones,'artifact_files':[a['filename'] for a in artifacts if a['metadata'].get('stream_id')==track.stream_id]})
            track_results.append(meta)
        pcm_signals = self._extract_pcm_signals(path)
        pcm_audio_tracks = self._write_pcm_artifacts(pcm_signals, pcm_result, output_dir)
        for item in pcm_audio_tracks:
            artifacts.extend(item.pop('_artifacts'))
            media_events.extend(item.pop('_events'))
        correlations = self._correlate_pcm_rtp(pcm_signals, tracks)
        scoped_audio_events = self._active_media_audio_events(pcm_signals, packet_result, pcm_result, tracks, correlations)
        candidate_clips = self._write_candidate_decision_clips(scoped_audio_events, pcm_signals, output_dir)
        artifacts.extend(candidate_clips['artifacts']); media_events.extend(candidate_clips['events'])
        echo_events = self._pcm_echo_events(pcm_signals)
        cross = []
        try:
            from app.analyzers.correlation import correlate_pcm_dtmf_with_sip
            cross = correlate_pcm_dtmf_with_sip(packet_result, pcm_result)
        except Exception:
            cross = []
        periodic_paths = build_periodic_path_analysis(pcm_signals, tracks, correlations, packet_result)
        periodic_artifacts = self._write_periodic_artifacts(periodic_paths, pcm_signals, tracks, output_dir)
        artifacts.extend(periodic_artifacts)
        cross.extend(periodic_paths)
        cross.extend(scoped_audio_events)
        cross.extend(echo_events)
        timeline = build_unified_timeline(packet_result, pcm_result, media_events, correlations + cross)
        candidate_decisions=[(e.get('details') or {}).get('candidate_decision') for e in scoped_audio_events if (e.get('details') or {}).get('candidate_decision')]
        return {
            'analyzer': self.analyzer_name,
            'version': self.analyzer_version,
            'status':'PARTIAL_SUCCESS' if degraded_reason or not self.pcm_profile.can_decode else 'SUCCESS',
            'analyzer_profile': self.analyzer_profile.metadata(),
            'pcm_profile': self.pcm_profile.metadata(),
            'degraded_reason': degraded_reason,
            'summary': {
                'call_count': packet_result.get('summary',{}).get('call_count') if packet_result.get('availability',{}).get('sip')!='UNAVAILABLE' else None,
                'rtp_stream_count': len(packet_result.get('rtp_streams',[])),
                'decoded_rtp_track_count': len(track_results),
                'pcm_session_count': pcm_result.get('summary',{}).get('session_count',0),
                'pcm_rtp_high_correlation_count': sum(1 for c in correlations if c.get('details',{}).get('correlation',{}).get('quality')=='HIGH'),
                'periodic_interference_count': sum(1 for e in periodic_paths if e.get('type')=='LOCAL_CAPTURE_PERIODIC_INTERFERENCE'),
                'unexpected_silence_candidate_count': sum(1 for e in scoped_audio_events if e.get('type')=='UNEXPECTED_SILENCE'),
                'unexpected_silence_count': sum(1 for e in scoped_audio_events if e.get('type')=='UNEXPECTED_SILENCE' and ((e.get('details') or {}).get('candidate_decision') or {}).get('status')=='PROMOTED'),
                'click_pop_candidate_count': sum(1 for e in scoped_audio_events if e.get('type')=='CLICK_POP'),
                'click_pop_count': sum(1 for e in scoped_audio_events if e.get('type')=='CLICK_POP' and ((e.get('details') or {}).get('candidate_decision') or {}).get('status')=='PROMOTED'),
                'candidate_promoted_count': sum(1 for d in candidate_decisions if d.get('status')=='PROMOTED'),
                'candidate_suppressed_count': sum(1 for d in candidate_decisions if d.get('status')=='SUPPRESSED'),
                'candidate_inconclusive_count': sum(1 for d in candidate_decisions if d.get('status')=='INCONCLUSIVE'),
                'echo_path_count': sum(1 for e in echo_events if e.get('type')=='ECHO_PATH_DETECTED'),
                'timeline_event_count': len(timeline),
                'artifact_count': len(artifacts),
            },
            'packet': packet_result,
            'pcm': pcm_result,
            'rtp_audio_tracks': track_results,
            'pcm_audio_tracks': pcm_audio_tracks,
            'correlations': correlations,
            'cross_layer_events': cross,
            'periodic_interference_paths': periodic_paths,
            'active_media_audio_events': scoped_audio_events,
            'candidate_decisions': candidate_decisions,
            'echo_paths': echo_events,
            'timeline': timeline,
            'artifacts': artifacts,
        }

    @staticmethod
    def _pcm_result_session(pcm_result: dict, tap_name: str, session_index: int) -> dict | None:
        for stream in pcm_result.get('streams',[]) or []:
            if (stream.get('tap') or {}).get('name') != tap_name:
                continue
            for session in stream.get('sessions',[]) or []:
                if session.get('session_index') == session_index:
                    return session
        return None

    @staticmethod
    def _best_pcm_rtp_correlation(correlations: list[dict], *, tap_name: str, session_index: int) -> tuple[str | None, float | None, str | None, float]:
        best=None
        for item in correlations:
            if item.get('type') != 'PCM_RTP_CORRELATION':
                continue
            details=item.get('details') or {}
            if details.get('pcm_tap') != tap_name or details.get('pcm_session_index') != session_index:
                continue
            corr=details.get('correlation') or {}; score=abs(float(corr.get('absolute_correlation') or 0.0))
            if best is None or score > best[0]:
                best=(score,details.get('rtp_stream_id'),corr.get('quality'),float(corr.get('lag_ms') or 0.0))
        if best is None:
            return None,None,None,0.0
        return best[1],best[0],best[2],best[3]

    def _active_media_audio_events(self, pcm_signals: list[dict], packet_result: dict, pcm_result: dict,
                                   tracks: list[RenderedRtpTrack], correlations: list[dict]) -> list[dict]:
        """Classify active-media audio candidates with lag-aligned cross-layer controls."""
        calls=[c for c in packet_result.get('calls',[]) if c.get('media_start_time') is not None and c.get('media_end_time') is not None]
        min_active=float(self.analyzer_profile.section('silence')['active_media_min_seconds'])
        track_by_id={t.stream_id:t for t in tracks}
        events=[]
        for pcm in pcm_signals:
            tap_name=pcm['tap']['name']; session_index=pcm['session_index']
            session_result=self._pcm_result_session(pcm_result,tap_name,session_index) or {}
            dtmf_intervals=[]
            for dtmf in session_result.get('dtmf_events',[]) or []:
                ds=pcm['start_time']+float(dtmf.get('start_seconds') or 0.0)
                de=pcm['start_time']+float(dtmf.get('end_seconds') if dtmf.get('end_seconds') is not None else dtmf.get('start_seconds') or 0.0)
                dtmf_intervals.append({'digit':dtmf.get('digit'),'start':ds,'end':de,'confidence':dtmf.get('confidence')})
            stream_id,corr_score,corr_quality,corr_lag_ms=self._best_pcm_rtp_correlation(correlations,tap_name=tap_name,session_index=session_index)
            counterpart=track_by_id.get(stream_id) if stream_id else None
            counterpart_has_synthetic_gaps=bool(counterpart and (counterpart.inserted_loss_samples>0 or counterpart.missing_payload_packets>0))
            for call in calls:
                start=max(float(pcm['start_time']),float(call['media_start_time']))
                end=min(float(pcm['end_time']),float(call['media_end_time']))
                if end-start < min_active:
                    continue
                sr=pcm['sample_rate']; a=max(0,int(round((start-pcm['start_time'])*sr))); b=min(pcm['samples'].size,int(round((end-pcm['start_time'])*sr)))
                chunk=pcm['samples'][a:b]
                if chunk.size < sr//2:
                    continue
                scope={'call_id':call.get('call_id'),'pcm_tap':tap_name,'pcm_session_index':session_index,'active_media_window':{'start_time':start,'end_time':end}}
                for ev in detect_unexpected_silence(chunk,sr):
                    abs_start=start+float(ev['start_seconds'])
                    abs_end=start+float(ev.get('end_seconds',ev['start_seconds']))
                    aligned_start=abs_start-corr_lag_ms/1000.0
                    aligned_end=abs_end-corr_lag_ms/1000.0
                    counterpart_metrics=None
                    if counterpart is not None and not counterpart_has_synthetic_gaps:
                        metrics=audio_window_metrics(counterpart.samples,counterpart.sample_rate,counterpart.start_time,aligned_start,aligned_end)
                        if metrics.get('sample_count',0)>0:
                            metrics.update({'pcm_absolute_start_time':abs_start,'pcm_absolute_end_time':abs_end,
                                            'correlation_lag_ms':corr_lag_ms,
                                            'alignment_rule':'rtp_window = pcm_window - correlation_lag'})
                            counterpart_metrics=metrics
                    decision=decide_silence(ev,absolute_start=abs_start,absolute_end=abs_end,scope=scope,
                                            counterpart_stream_id=stream_id,counterpart_correlation=corr_score,
                                            counterpart_metrics=counterpart_metrics)
                    evidence=decision.setdefault('positive_evidence',{})
                    evidence['correlation_quality']=corr_quality
                    evidence['correlation_lag_ms']=corr_lag_ms
                    evidence['counterpart_aligned_start_time']=aligned_start
                    evidence['counterpart_aligned_end_time']=aligned_end
                    evidence['counterpart_inserted_loss_samples']=counterpart.inserted_loss_samples if counterpart else None
                    evidence['counterpart_missing_payload_packets']=counterpart.missing_payload_packets if counterpart else None
                    if counterpart_has_synthetic_gaps:
                        decision['status']='INCONCLUSIVE'
                        decision['reason_code']='RTP_COUNTERPART_CONTAINS_SYNTHETIC_GAPS'
                    events.append({'type':'UNEXPECTED_SILENCE','time':abs_start,'start_time':abs_start,'end_time':abs_end,
                                   'severity':'MEDIUM','evidence_level':'L2','scope':scope,
                                   'details':{**ev,'absolute_start_time':abs_start,'absolute_end_time':abs_end,
                                              'candidate_decision':decision,
                                              'interpretation':'活跃媒体静音候选已按 PCM↔RTP correlation lag 对齐执行跨层 CandidateDecision；只有对应 RTP 源窗口明确活动且无合成补零时才升级为异常 Finding。'}})
                for ev in detect_click_pop_robust(chunk,sr):
                    abs_t=start+float(ev['time_seconds'])
                    decision=decide_click_pop(ev,absolute_time=abs_t,scope=scope,dtmf_intervals=dtmf_intervals,media_start=start,media_end=end)
                    events.append({'type':'CLICK_POP','time':abs_t,'start_time':abs_t,'end_time':abs_t,
                                   'severity':'MEDIUM','evidence_level':'L3','scope':scope,
                                   'details':{**ev,'absolute_time':abs_t,'candidate_decision':decision,
                                              'interpretation':'活跃媒体 Click/Pop 候选已执行 DTMF、媒体边界及置信度 Negative Control；仅 PROMOTED 候选进入异常 Finding。'}})
        return events

    def _pcm_echo_events(self, pcm_signals: list[dict]) -> list[dict]:
        echo_cfg=self.analyzer_profile.section('echo'); min_overlap=float(echo_cfg['min_overlap_seconds'])
        rx=[p for p in pcm_signals if str(p['tap'].get('direction','')).upper()=='RX']
        tx=[p for p in pcm_signals if str(p['tap'].get('direction','')).upper()=='TX']
        events=[]
        for ref in tx:
            for obs in rx:
                start=max(ref['start_time'],obs['start_time']); end=min(ref['end_time'],obs['end_time'])
                if end-start < min_overlap:
                    continue
                sr=ref['sample_rate']
                if obs['sample_rate']!=sr:
                    continue
                ra=max(0,int(round((start-ref['start_time'])*sr))); rb=min(ref['samples'].size,int(round((end-ref['start_time'])*sr)))
                oa=max(0,int(round((start-obs['start_time'])*sr))); ob=min(obs['samples'].size,int(round((end-obs['start_time'])*sr)))
                n=min(rb-ra,ob-oa)
                if n < sr*2:
                    continue
                result=analyze_echo_path(ref['samples'][ra:ra+n],obs['samples'][oa:oa+n],sr)
                if not result.get('detected'):
                    continue
                events.append({'type':'ECHO_PATH_DETECTED','time':start,'severity':'MEDIUM' if result.get('quality')=='HIGH' else 'INFO','evidence_level':result.get('evidence_level','L3'),'scope':{'reference_pcm_tap':ref['tap']['name'],'reference_session_index':ref['session_index'],'observed_pcm_tap':obs['tap']['name'],'observed_session_index':obs['session_index'],'overlap_start_time':start,'overlap_end_time':end},'details':result})
        events.sort(key=lambda e:(-float(e['details'].get('absolute_correlation',0)),e['time']))
        return events[:20]

    def _write_event_clips(self, track: RenderedRtpTrack, rtp_events: list[dict], output_dir: Path, base: str) -> dict:
        artifacts=[]; events=[]
        clip_count=0
        for n, event in enumerate(rtp_events):
            if event.get('type') not in {'PACKET_LOSS','BURST_LOSS','HIGH_DELTA'}:
                continue
            if clip_count >= 10:
                break
            abs_t=float(event.get('start_time', track.start_time))
            rel=max(0.0, abs_t-track.start_time)
            pre=0.8; post=1.2
            a=max(0,int((rel-pre)*track.sample_rate)); b=min(track.samples.size,int((rel+post)*track.sample_rate))
            if b<=a: continue
            path=output_dir/f'{base}_event_{n:03d}_{event.get("type","EVENT")}.wav'
            write_wav(path, track.samples[a:b], track.sample_rate, track.channels)
            artifacts.append(_artifact(path,'AUDIO_CLIP','audio/wav',{'stream_id':track.stream_id,'event_type':event.get('type'),'event_time':abs_t,'clip_start_relative':round(a/track.sample_rate,6)}))
            clip_count += 1
            events.append({'time':abs_t,'source':'RTP_AUDIO','type':'AUDIO_CLIP_CREATED','severity':'INFO','details':{'stream_id':track.stream_id,'rtp_event_type':event.get('type'),'filename':path.name,'note':'原始媒体内容片段；不会模拟接收端抖动缓冲行为'}})
        return {'artifacts':artifacts,'events':events}

    def _write_pcm_artifacts(self, pcm_signals: list[dict], pcm_result: dict, output_dir: Path) -> list[dict]:
        out=[]
        for pcm in pcm_signals:
            tap=pcm['tap']['name']; idx=pcm['session_index']; base=f'pcm_{tap}_{idx:02d}'
            wav=output_dir/f'{base}.wav'; write_wav(wav,pcm['samples'],pcm['sample_rate'],1)
            wave_json=output_dir/f'{base}_waveform.json'; wave_json.write_text(json.dumps(waveform_envelope(pcm['samples'],pcm['sample_rate']),ensure_ascii=False,separators=(',',':')))
            spec_json=output_dir/f'{base}_spectrogram.json'; spec_json.write_text(json.dumps(spectrogram_data(pcm['samples'],pcm['sample_rate']),ensure_ascii=False,separators=(',',':')))
            artifacts=[
                _artifact(wav,'PCM_WAV','audio/wav',{'pcm_tap':tap,'session_index':idx}),
                _artifact(wave_json,'WAVEFORM_JSON','application/json',{'pcm_tap':tap,'session_index':idx}),
                _artifact(spec_json,'SPECTROGRAM_JSON','application/json',{'pcm_tap':tap,'session_index':idx}),
            ]
            out.append({'pcm_tap':tap,'direction':pcm['tap']['direction'],'session_index':idx,'start_time':pcm['start_time'],'end_time':pcm['end_time'],'sample_rate':pcm['sample_rate'],'duration_seconds':round(pcm['samples'].size/pcm['sample_rate'],6),'artifact_files':[a['filename'] for a in artifacts],'_artifacts':artifacts,'_events':[]})
        return out

    def _write_candidate_decision_clips(self, scoped_events: list[dict], pcm_signals: list[dict], output_dir: Path) -> dict:
        artifacts=[]; events=[]; lookup={(p['tap']['name'],p['session_index']):p for p in pcm_signals}; count=0
        for event in scoped_events:
            details=event.get('details') or {}; decision=details.get('candidate_decision') or {}
            if decision.get('status')!='PROMOTED' or event.get('type') not in {'CLICK_POP','UNEXPECTED_SILENCE'}:
                continue
            if count>=10:
                break
            scope=event.get('scope') or {}; pcm=lookup.get((scope.get('pcm_tap'),scope.get('pcm_session_index')))
            if pcm is None:
                continue
            start=float(event.get('start_time',event.get('time',pcm['start_time']))); end=float(event.get('end_time',start))
            if event.get('type')=='CLICK_POP':
                clip_start=start-0.5; clip_end=start+0.5
            else:
                clip_start=start-1.0; clip_end=end+1.0
            sr=pcm['sample_rate']; a=max(0,int(round((clip_start-pcm['start_time'])*sr))); b=min(pcm['samples'].size,int(round((clip_end-pcm['start_time'])*sr)))
            if b<=a:
                continue
            cid=str(decision.get('candidate_id') or f'candidate-{count}')
            path=output_dir/f"candidate_{count:03d}_{event.get('type')}_{cid[-8:]}.wav"
            write_wav(path,pcm['samples'][a:b],sr,1)
            positive=decision.get('positive_evidence') or {}
            meta={'pcm_tap':scope.get('pcm_tap'),'session_index':scope.get('pcm_session_index'),'event_type':event.get('type'),
                  'event_time':event.get('time'),'candidate_id':decision.get('candidate_id'),'candidate_decision_status':'PROMOTED',
                  'candidate_reason_code':decision.get('reason_code'),'correlation_lag_ms':positive.get('correlation_lag_ms'),
                  'clip_start_time':pcm['start_time']+a/sr,'clip_end_time':pcm['start_time']+b/sr}
            artifacts.append(_artifact(path,'AUDIO_CLIP','audio/wav',meta)); count+=1
            events.append({'time':event.get('time'),'source':scope.get('pcm_tap'),'type':'AUDIO_CLIP_CREATED','severity':'INFO',
                           'details':{'event_type':event.get('type'),'filename':path.name,'candidate_id':decision.get('candidate_id'),'candidate_decision_status':'PROMOTED'}})
        return {'artifacts':artifacts,'events':events}

    def _extract_pcm_signals(self, path: str | Path) -> list[dict]:
        if not self.pcm_profile.can_decode:
            return []
        taps={t.dst_port:t for t in self.pcm_profile.taps}; groups={t.name:[] for t in self.pcm_profile.taps}
        for d in iter_udp_datagrams(path):
            tap=taps.get(d.dst_port)
            size_ok=self.pcm_profile.packet_payload_bytes is None or len(d.payload)==self.pcm_profile.packet_payload_bytes
            if tap and size_ok:
                groups[tap.name].append(d)
        result=[]; pcm_engine=PcmIntelligenceEngine(self.pcm_profile)
        for tap in self.pcm_profile.taps:
            sessions=pcm_engine._split_sessions(groups[tap.name])
            for i, packets in enumerate(sessions):
                raw=b''.join(pcm_engine._payload_bytes(p) for p in packets)
                itemsize=np.dtype(self.pcm_profile.dtype).itemsize
                if len(raw)%itemsize:
                    raw=raw[:len(raw)-(len(raw)%itemsize)]
                samples=np.frombuffer(raw,dtype=self.pcm_profile.dtype).copy()
                result.append({'tap':asdict(tap),'session_index':i,'start_time':packets[0].timestamp,'end_time':packets[-1].timestamp,'samples':samples,'sample_rate':self.pcm_profile.sample_rate})
        return result

    def _write_periodic_artifacts(self, periodic_paths: list[dict], pcm_signals: list[dict], tracks: list[RenderedRtpTrack], output_dir: Path) -> list[dict]:
        artifacts=[]
        pcm_lookup={(p['tap']['name'],p['session_index']):p for p in pcm_signals}
        track_lookup={t.stream_id:t for t in tracks}
        for idx,event in enumerate(periodic_paths):
            if event.get('type')!='LOCAL_CAPTURE_PERIODIC_INTERFERENCE':
                continue
            scope=event.get('scope') or {}; details=event.get('details') or {}; files=[]
            specs=[
                ('pcm_rx',pcm_lookup.get((scope.get('pcm_tap'),scope.get('pcm_session_index'))),details.get('pcm_rx')),
                ('rtp_up',track_lookup.get(scope.get('upstream_rtp_stream_id')),details.get('upstream_rtp')),
                ('rtp_down',track_lookup.get(scope.get('downstream_rtp_stream_id')),details.get('downstream_rtp')),
            ]
            for label,obj,analysis in specs:
                rep=(analysis or {}).get('representative') or {}
                absolute=rep.get('absolute_start_time')
                duration=float(rep.get('duration_seconds') or 1.0)
                if obj is None or absolute is None:
                    continue
                if isinstance(obj,RenderedRtpTrack):
                    samples=slice_by_absolute_time(obj.samples,obj.sample_rate,obj.start_time,float(absolute),float(absolute)+duration); sr=obj.sample_rate
                else:
                    samples=slice_by_absolute_time(obj['samples'],obj['sample_rate'],obj['start_time'],float(absolute),float(absolute)+duration); sr=obj['sample_rate']
                if samples.size==0:
                    continue
                wav=output_dir/f'periodic_{idx:02d}_{label}.wav'; write_wav(wav,samples,sr,1); files.append(wav.name)
                artifacts.append(_artifact(wav,'PERIODIC_AUDIO_CLIP','audio/wav',{'event_type':'LOCAL_CAPTURE_PERIODIC_INTERFERENCE','path_index':idx,'source':label,'scope':scope}))
            metrics=output_dir/f'periodic_{idx:02d}_metrics.json'; metrics.write_text(json.dumps(event,ensure_ascii=False,indent=2),encoding='utf-8'); files.append(metrics.name)
            artifacts.append(_artifact(metrics,'PERIODIC_METRICS_JSON','application/json',{'event_type':'LOCAL_CAPTURE_PERIODIC_INTERFERENCE','path_index':idx,'scope':scope}))
            event.setdefault('details',{})['artifact_files']=files
        return artifacts

    def _correlate_pcm_rtp(self, pcm_signals: list[dict], tracks: list[RenderedRtpTrack]) -> list[dict]:
        out=[]
        corr_cfg=self.analyzer_profile.section('correlation')
        min_overlap=float(corr_cfg['min_overlap_seconds']); emit_min=float(corr_cfg['emit_min_correlation'])
        for pcm in pcm_signals:
            for track in tracks:
                overlap=min(pcm['end_time'],track.end_time)-max(pcm['start_time'],track.start_time)
                if overlap < min_overlap: continue
                corr=correlate_tracks(pcm['samples'],pcm['sample_rate'],pcm['start_time'],track.samples,track.sample_rate,track.start_time)
                if not corr or corr['absolute_correlation'] < emit_min: continue
                out.append({'type':'PCM_RTP_CORRELATION','time':max(pcm['start_time'],track.start_time),'details':{
                    'pcm_tap':pcm['tap']['name'],'pcm_direction':pcm['tap']['direction'],'pcm_session_index':pcm['session_index'],
                    'rtp_stream_id':track.stream_id,'rtp_direction':f'{track.src_ip}:{track.src_port}->{track.dst_ip}:{track.dst_port}',
                    'correlation':corr,
                    'interpretation':'高相关表示该PCM Tap与该RTP方向携带相同/高度相似的语音内容；用于定位媒体链路方向，不单独作为硬件根因。'
                }})
        out.sort(key=lambda x:x['details']['correlation']['absolute_correlation'], reverse=True)
        return out


def _artifact(path: Path, artifact_type: str, content_type: str, metadata: dict) -> dict:
    return {'filename':path.name,'local_path':str(path),'type':artifact_type,'content_type':content_type,'metadata':metadata}
