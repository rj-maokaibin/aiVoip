import math
import subprocess
import struct
import wave

import numpy as np

from app.analyzers.attachments import (_redact_ocr_text, _structured_ocr_observations, align_field_audio,
                                       analyze_field_audio, analyze_field_wav, inspect_image)
from app.diagnosis.reasoner import DeterministicDiagnosisReasoner
from app.workers.device_provision_task import _attachment_evidence_type


def test_field_wav_analysis_is_non_confirmatory(tmp_path):
    rate = 8000
    samples = (np.sin(2 * math.pi * 1000 * np.arange(rate) / rate) * 10000).astype('<i2')
    path = tmp_path / 'field.wav'
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(rate); out.writeframes(samples.tobytes())
    result = analyze_field_wav(path)
    assert result['status'] == 'SUCCESS'
    assert result['summary']['duration_seconds'] == 1.0
    assert result['summary']['sample_rate'] == rate
    assert result['summary']['availability'] == 'ANALYZED'
    assert result['limitations']


def test_image_metadata_only_never_claims_content(tmp_path):
    path = tmp_path / 'screen.png'
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\x0dIHDR' + struct.pack('>II', 640, 480))
    result = inspect_image(path, tesseract_binary='/definitely/missing/tesseract')
    assert result['status'] == 'PARTIAL_SUCCESS'
    assert result['summary']['availability'] == 'METADATA_ONLY'
    assert result['summary']['width'] == 640 and result['summary']['height'] == 480
    assert result['summary']['ocr_availability'] == 'UNAVAILABLE'
    assert 'OCR' in result['limitations'][0]


def test_feishu_attachment_classification():
    assert _attachment_evidence_type('record.wav') == 'FIELD_AUDIO_WAV'
    assert _attachment_evidence_type('voice.opus', 'audio', 'audio/ogg') == 'FIELD_AUDIO'
    assert _attachment_evidence_type('image.bin', 'image', 'image/png') == 'FIELD_IMAGE'


def test_reasoner_schedules_and_consumes_attachment_analysis():
    base = {'case': {'id': 'c', 'summary': '电话有杂音'}, 'devices': [],
            'evidences': [{'id': 'e1', 'type': 'FIELD_AUDIO_WAV', 'filename': 'field.wav', 'metadata': {}}],
            'analyzers': {}}
    first = DeterministicDiagnosisReasoner().reason(base)
    assert any(x.action_type == 'RUN_FIELD_AUDIO_ANALYSIS' for x in first.plan)
    result = {'status': 'SUCCESS', 'summary': {'duration_seconds': 2.0, 'sample_rate': 8000, 'rms_dbfs': -20},
              'findings': [{'type': 'FIELD_RECORDING_NARROWBAND_TONE_CANDIDATE'}]}
    second = DeterministicDiagnosisReasoner().reason({**base, 'analyzers': {
        'field_audio_intelligence': {'run_id': 'r1', 'input_evidence_ids':['e1'], 'result': result}}})
    assert not any(x.action_type == 'RUN_FIELD_AUDIO_ANALYSIS' for x in second.plan)
    assert any(x.code == 'FIELD_RECORDING_NARROWBAND_TONE' and x.status == 'OPEN' for x in second.hypotheses)
    assert any(x.action_type == 'REQUEST_USER_EVIDENCE' for x in second.plan)


def test_reasoner_does_not_retry_unavailable_audio_decoder():
    snapshot = {'case': {'id': 'c', 'summary': '电话有杂音'}, 'devices': [],
                'evidences': [{'id': 'e1', 'type': 'FIELD_AUDIO_WAV', 'filename': 'compressed.wav', 'metadata': {}}],
                'analyzers': {'field_audio_intelligence': {'run_id': 'r1', 'input_evidence_ids':['e1'], 'result': {
                    'status': 'PARTIAL_SUCCESS',
                    'summary': {'availability': 'DECODE_UNAVAILABLE', 'reason': 'WAV_COMPRESSION_UNSUPPORTED'},
                    'findings': []}}}}
    decision = DeterministicDiagnosisReasoner().reason(snapshot)
    assert not any(x.action_type == 'RUN_FIELD_AUDIO_ANALYSIS' for x in decision.plan)
    question = next(x for x in decision.plan if x.action_type == 'REQUEST_USER_EVIDENCE')
    assert question.params['need'] == ['supported_audio_recording']


def test_field_audio_wav_uses_native_decoder(tmp_path):
    rate=8000; samples=(np.sin(2*math.pi*440*np.arange(rate*2)/rate)*8000).astype('<i2')
    path=tmp_path/'native.wav'
    with wave.open(str(path),'wb') as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(rate); out.writeframes(samples.tobytes())
    assert analyze_field_audio(path)['summary']['format']=='WAV_PCM'


def test_field_media_alignment_maps_events_to_capture_time(tmp_path):
    rate=8000; rng=np.random.default_rng(7); source=rng.integers(-12000,12000,rate*3,dtype=np.int16)
    track=tmp_path/'track.wav'; field=tmp_path/'field.wav'
    for path,samples in [(track,source),(field,np.r_[np.zeros(rate//2,dtype=np.int16),source])]:
        with wave.open(str(path),'wb') as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(rate); out.writeframes(samples.astype('<i2').tobytes())
    result=align_field_audio(field,[{'path':str(track),'source':'RTP','stream_id':'s1','start_time':1000.0,
                                     'field_events':[{'type':'CLICK_POP','time_seconds':1.0}]}],
                             [{'call_id':'call-1','media_start_time':999.0,'media_end_time':1005.0}])
    assert result['summary']['availability']=='ALIGNED'
    best=result['alignments'][0]
    assert best['correlation']['quality']=='HIGH'
    assert best['mapped_events'][0]['call_id']=='call-1'


def test_reasoner_schedules_cross_media_alignment():
    snapshot={'case':{'id':'c','summary':'电话有杂音'},'devices':[],
              'evidences':[{'id':'audio','type':'FIELD_AUDIO','filename':'voice.ogg','metadata':{}},
                           {'id':'pcap','type':'PCAP','filename':'call.pcap','metadata':{}}],
              'analyzers':{
                  'field_audio_intelligence':{'run_id':'ar','input_evidence_ids':['audio'],'result':{'summary':{'availability':'ANALYZED'},'findings':[]}},
                  'media_intelligence':{'run_id':'mr','result':{'packet':{},'summary':{}}}}}
    decision=DeterministicDiagnosisReasoner().reason(snapshot)
    action=next(x for x in decision.plan if x.action_type=='RUN_FIELD_MEDIA_ALIGNMENT')
    assert action.params=={'evidence_id':'audio','media_run_id':'mr'}


def test_ocr_candidates_are_bounded_and_secrets_redacted():
    text=_redact_ocr_text('注册状态：已注册\nCodec: G711A\npassword: do-not-store')
    assert 'do-not-store' not in text
    observations={x['key']:x['value'] for x in _structured_ocr_observations(text)}
    assert observations['registration_status']=='已注册'
    assert observations['codec']=='G711A'


def test_new_attachment_is_not_hidden_by_previous_analyzer_run():
    snapshot={'case':{'id':'c','summary':'杂音'},'devices':[],
              'evidences':[{'id':'old','type':'FIELD_AUDIO_WAV','filename':'old.wav','metadata':{}},
                           {'id':'new','type':'FIELD_AUDIO','filename':'new.ogg','metadata':{}}],
              'analyzers':{'field_audio_intelligence':{'run_id':'r1','input_evidence_ids':['old'],
                  'result':{'summary':{'availability':'ANALYZED'},'findings':[]}}}}
    decision=DeterministicDiagnosisReasoner().reason(snapshot)
    action=next(x for x in decision.plan if x.action_type=='RUN_FIELD_AUDIO_ANALYSIS')
    assert action.params['evidence_id']=='new'


def test_ffmpeg_decodes_real_opus_ogg(tmp_path):
    rate=16000; samples=(np.sin(2*math.pi*660*np.arange(rate*2)/rate)*7000).astype('<i2')
    wav=tmp_path/'source.wav'; ogg=tmp_path/'voice.ogg'
    with wave.open(str(wav),'wb') as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(rate); out.writeframes(samples.tobytes())
    subprocess.run(['ffmpeg','-v','error','-i',str(wav),'-c:a','libopus','-y',str(ogg)],check=True)
    result=analyze_field_audio(ogg)
    assert result['status']=='SUCCESS'
    assert result['summary']['source_format']=='ogg'
    assert result['summary']['decoded_by']=='ffmpeg'


def test_tesseract_extracts_codec_candidate_from_rendered_screen(tmp_path):
    image=tmp_path/'device-screen.png'
    subprocess.run([
        'ffmpeg','-v','error','-f','lavfi','-i','color=white:s=1000x240:d=1','-vf',
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:text='Codec\\: G711A':fontcolor=black:fontsize=64:x=50:y=80",
        '-frames:v','1','-threads','1','-y',str(image),
    ],check=True)
    result=inspect_image(image)
    assert result['ocr']['availability']=='EXTRACTED'
    assert {'key':'codec','value':'G711A','evidence_level':'L4','status':'OCR_CANDIDATE'} in result['ocr']['observations']
    assert result['visual_semantics']['availability']=='CANDIDATE_ONLY'
    assert all(x['evidence_level']=='L4' for x in result['visual_semantics']['observations'])
    assert result['visual_semantics']['semantic_labels_assigned'] is False


def test_aligned_recording_does_not_keep_not_aligned_unknown():
    snapshot={'case':{'id':'c','summary':'电话有杂音'},'devices':[],
              'evidences':[{'id':'audio','type':'FIELD_AUDIO_WAV','filename':'field.wav','metadata':{}},
                           {'id':'pcap','type':'PCAP','filename':'call.pcap','metadata':{}}],
              'analyzers':{
                  'field_audio_intelligence':{'run_id':'ar','input_evidence_ids':['audio'],
                      'result':{'summary':{'availability':'ANALYZED','duration_seconds':2,'sample_rate':8000,'rms_dbfs':-20},'findings':[]}},
                  'media_intelligence':{'run_id':'mr','input_evidence_ids':['pcap'],'result':{'packet':{},'summary':{}}},
                  'field_media_alignment':{'run_id':'xr','input_evidence_ids':['audio','pcap'],
                      'config_snapshot':{'media_run_id':'mr'},'result':{'summary':{'availability':'ALIGNED'},
                      'alignments':[{'source':'RTP','correlation':{'quality':'HIGH','absolute_correlation':0.91,'lag_ms':400},'mapped_events':[]}]}}}}
    decision=DeterministicDiagnosisReasoner().reason(snapshot)
    assert any('现场录音已与 RTP 媒体对齐' in x for x in decision.known)
    assert not any('尚未与同一通话' in x for x in decision.unknown)
    assert not any(x.action_type=='RUN_FIELD_MEDIA_ALIGNMENT' for x in decision.plan)
