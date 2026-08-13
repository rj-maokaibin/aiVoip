from __future__ import annotations
from collections import Counter
from app.diagnosis.triage import triage_summary


def build_rule_facts(snapshot:dict) -> dict:
    summary=(snapshot.get('case') or {}).get('summary','')
    symptoms=triage_summary(summary)
    analyzers=snapshot.get('analyzers') or {}
    media=analyzers.get('media_intelligence') or {}
    packet=analyzers.get('packet_intelligence') or {}
    result=(media.get('result') or packet.get('result') or {})
    packet_result=result.get('packet',result) if isinstance(result,dict) else {}
    anomalies=packet_result.get('anomalies',[]) or []
    counts=Counter(str(x.get('type')) for x in anomalies)
    cross=result.get('cross_layer_events',[]) if isinstance(result,dict) else []
    cross_counts=Counter(str(x.get('type')) for x in (cross or []))
    regs=packet_result.get('registrations',[]) or []
    calls=packet_result.get('calls',[]) or []
    rtp=packet_result.get('rtp_streams',[]) or []
    pcm=result.get('pcm',{}) if isinstance(result,dict) else {}
    hum=clicks=silences=0
    for stream in pcm.get('streams',[]) or []:
        for sess in stream.get('sessions',[]) or []:
            spectral=sess.get('spectral') or sess.get('spectral_tone') or {}
            h=sess.get('hum') or {}
            if str(h.get('level','')).upper()=='HIGH' or str(spectral.get('hum_score','')).upper()=='HIGH': hum+=1
            clicks += len(sess.get('click_pop_events',[]) or [])
            silences += sum(1 for x in sess.get('silence_events',[]) or [] if float(x.get('duration_ms',0))>=200)
    correlations=result.get('correlations',[]) if isinstance(result,dict) else []
    high_corr=sum(1 for c in correlations or [] if ((c.get('details') or {}).get('correlation') or {}).get('quality')=='HIGH')
    source=packet_result.get('source',{}) if isinstance(packet_result,dict) else {}
    availability=packet_result.get('availability',{}) if isinstance(packet_result,dict) else {}
    sip_available=availability.get('sip')!='UNAVAILABLE'
    return {
        'case':{'has_evidence':bool(snapshot.get('evidences')),'device_count':len(snapshot.get('devices') or [])},
        'symptoms':{k:True for k in symptoms},
        'anomaly_counts':dict(counts),
        'cross_event_counts':dict(cross_counts),
        'sip':{
            'available':sip_available,
            'registration_count':len(regs) if sip_available else None,
            'registration_failed_count':sum(1 for x in regs if x.get('status')=='FAILED') if sip_available else None,
            'call_count':len(calls) if sip_available else None,
            'call_failed_count':sum(1 for x in calls if x.get('state')=='FAILED') if sip_available else None,
        },
        'rtp':{'stream_count':len(rtp),'has_loss':bool(counts.get('PACKET_LOSS') or counts.get('BURST_LOSS')),'has_jitter':bool(counts.get('HIGH_DELTA') or counts.get('HIGH_JITTER')),'one_way_media_count':counts.get('ONE_WAY_RTP_MEDIA',0)},
        'pcm':{
            'hum_high_count':hum,
            'click_count':clicks,
            'long_silence_count':silences,
            'local_capture_periodic_interference_count':cross_counts.get('LOCAL_CAPTURE_PERIODIC_INTERFERENCE',0),
        },
        'media':{
            'high_correlation_count':high_corr,
            'dtmf_sip_match_count':cross_counts.get('DTMF_SIP_DIAL_MATCH',0),
            'dtmf_sip_mismatch_count':cross_counts.get('DTMF_SIP_DIAL_MISMATCH',0),
            'periodic_interference_path_count':len(result.get('periodic_interference_paths',[]) or []) if isinstance(result,dict) else 0,
            'unexpected_silence_count':cross_counts.get('UNEXPECTED_SILENCE',0),
            'click_pop_count':cross_counts.get('CLICK_POP',0),
            'echo_path_count':cross_counts.get('ECHO_PATH_DETECTED',0),
        },
        'capture':{'multi_point':bool(source.get('capture_points') and len(source.get('capture_points'))>1)},
    }
