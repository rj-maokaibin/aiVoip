from __future__ import annotations

import math
import struct
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

from app.analyzers.audio.features import detect_silence, spectral_tone_analysis, waveform_envelope
from app.analyzers.audio.quality import detect_click_pop_robust


def _dbfs(value: float) -> float:
    return -120.0 if value <= 0 else 20.0 * math.log10(value / 32768.0)


def read_wav_mono(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), 'rb') as wav:
        channels=wav.getnchannels(); rate=wav.getframerate(); width=wav.getsampwidth(); frames=wav.getnframes()
        if wav.getcomptype()!='NONE' or width not in {1,2,4}:
            raise ValueError('WAV_PCM_REQUIRED')
        raw=wav.readframes(frames)
    if width==1: values=(np.frombuffer(raw,dtype=np.uint8).astype(np.int16)-128)<<8
    elif width==2: values=np.frombuffer(raw,dtype='<i2').astype(np.int16,copy=False)
    else: values=(np.frombuffer(raw,dtype='<i4')>>16).astype(np.int16)
    if channels<1 or values.size%channels: raise ValueError('WAV_FRAME_ALIGNMENT_INVALID')
    return np.mean(values.reshape(-1,channels).astype(np.float64),axis=1).clip(-32768,32767).astype(np.int16),rate


def decode_field_audio_to_wav(path: Path, output: Path, *, ffmpeg_binary: str='ffmpeg') -> None:
    proc=subprocess.run([ffmpeg_binary,'-v','error','-nostdin','-i',str(path),'-vn','-map_metadata','-1',
                         '-ac','1','-ar','16000','-c:a','pcm_s16le','-y',str(output)],
                        text=True,capture_output=True,timeout=90,check=False)
    if proc.returncode!=0 or not output.exists(): raise ValueError('AUDIO_DECODE_UNAVAILABLE')


def analyze_field_wav(path: Path, *, max_duration_seconds: float = 600.0) -> dict:
    """Analyze an uncompressed PCM WAV without claiming system-side root cause."""
    try:
        with wave.open(str(path), 'rb') as wav:
            channels = wav.getnchannels()
            sample_rate = wav.getframerate()
            sample_width = wav.getsampwidth()
            frames = wav.getnframes()
            compression = wav.getcomptype()
            duration = frames / sample_rate if sample_rate else 0.0
            if compression != 'NONE':
                raise ValueError(f'WAV_COMPRESSION_UNSUPPORTED:{compression}')
            if channels < 1 or channels > 8 or sample_rate < 1000 or sample_rate > 192000:
                raise ValueError('WAV_FORMAT_INVALID')
            if sample_width not in {1, 2, 4}:
                raise ValueError(f'WAV_SAMPLE_WIDTH_UNSUPPORTED:{sample_width}')
            if duration <= 0 or duration > max_duration_seconds:
                raise ValueError('WAV_DURATION_INVALID')
            raw = wav.readframes(frames)
    except (wave.Error, EOFError) as exc:
        raise ValueError(f'WAV_DECODE_FAILED:{exc}') from exc

    if sample_width == 1:
        values = (np.frombuffer(raw, dtype=np.uint8).astype(np.int16) - 128) << 8
    elif sample_width == 2:
        values = np.frombuffer(raw, dtype='<i2').astype(np.int16, copy=False)
    else:
        values = (np.frombuffer(raw, dtype='<i4') >> 16).astype(np.int16)
    if values.size % channels:
        raise ValueError('WAV_FRAME_ALIGNMENT_INVALID')
    shaped = values.reshape(-1, channels)
    mono = np.mean(shaped.astype(np.float64), axis=1).clip(-32768, 32767).astype(np.int16)
    rms = float(np.sqrt(np.mean(mono.astype(np.float64) ** 2))) if mono.size else 0.0
    peak = float(np.max(np.abs(mono.astype(np.int32)))) if mono.size else 0.0
    clipping_ratio = float(np.mean(np.abs(mono.astype(np.int32)) >= 32700)) if mono.size else 0.0
    silence = detect_silence(mono, sample_rate)
    clicks = detect_click_pop_robust(mono, sample_rate)
    spectral = spectral_tone_analysis(mono, sample_rate)
    findings = []
    if clipping_ratio >= 0.005:
        findings.append({'type': 'FIELD_RECORDING_CLIPPED', 'evidence_level': 'L3',
                         'clipping_ratio': round(clipping_ratio, 6)})
    if _dbfs(rms) <= -55:
        findings.append({'type': 'FIELD_RECORDING_LOW_ENERGY', 'evidence_level': 'L3',
                         'rms_dbfs': round(_dbfs(rms), 3)})
    if clicks:
        findings.append({'type': 'FIELD_RECORDING_CLICK_POP_CANDIDATE', 'evidence_level': 'L3',
                         'count': len(clicks)})
    if spectral.get('narrowband_tones'):
        findings.append({'type': 'FIELD_RECORDING_NARROWBAND_TONE_CANDIDATE', 'evidence_level': 'L3',
                         'tones': spectral['narrowband_tones'][:5]})
    summary = {
        'availability': 'ANALYZED', 'format': 'WAV_PCM', 'duration_seconds': round(duration, 3),
        'sample_rate': sample_rate, 'channels': channels, 'sample_width_bits': sample_width * 8,
        'rms_dbfs': round(_dbfs(rms), 3), 'peak_dbfs': round(_dbfs(peak), 3),
        'clipping_ratio': round(clipping_ratio, 6), 'finding_count': len(findings),
    }
    return {
        'status': 'SUCCESS', 'summary': summary, 'findings': findings,
        'silence_segments': silence[:100], 'click_pop_events': clicks[:100], 'spectral': spectral,
        'waveform': waveform_envelope(mono, sample_rate),
        'limitations': [
            '现场单点录音只能描述听感信号特征，不能单独定位终端、线路、网络或PBX根因。',
            '录音未与SIP/RTP/PCM时间轴对齐，所有异常仅为候选，不作为确认性根因。',
        ],
    }


def analyze_raw_pcm(path: Path, pcm_format: dict) -> dict:
    """Interpret headerless PCM only when every required parameter is explicit."""
    try:
        rate=int(pcm_format['sample_rate'])
        width=int(pcm_format['sample_width_bits'])
        channels=int(pcm_format['channels'])
        signed=bool(pcm_format.get('signed',True))
        endian=str(pcm_format.get('endian','little')).lower()
    except (KeyError,TypeError,ValueError) as exc:
        raise ValueError('RAW_PCM_FORMAT_REQUIRED') from exc
    if rate<1000 or rate>192000 or width not in {8,16,32} or channels<1 or channels>8:
        raise ValueError('RAW_PCM_FORMAT_INVALID')
    if endian not in {'little','big'}:
        raise ValueError('RAW_PCM_ENDIAN_INVALID')
    raw=path.read_bytes(); bytes_per_sample=width//8
    if not raw or len(raw)%(bytes_per_sample*channels):
        raise ValueError('RAW_PCM_FRAME_ALIGNMENT_INVALID')
    if width==8:
        dtype=np.int8 if signed else np.uint8
    else:
        dtype=np.dtype(('>' if endian=='big' else '<')+('i' if signed else 'u')+str(bytes_per_sample))
    values=np.frombuffer(raw,dtype=dtype)
    if not signed:
        values=values.astype(np.int64)-(1<<(width-1))
    if width>16: values=(values.astype(np.int64)>>(width-16))
    elif width<16: values=(values.astype(np.int64)<<(16-width))
    mono=np.mean(values.reshape(-1,channels).astype(np.float64),axis=1).clip(-32768,32767).astype('<i2')
    with tempfile.TemporaryDirectory(prefix='raw-pcm-') as td:
        wav_path=Path(td)/'raw.wav'
        with wave.open(str(wav_path),'wb') as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(rate); out.writeframes(mono.tobytes())
        result=analyze_field_wav(wav_path)
    result['summary'].update({'source_format':'RAW_PCM','decoded_by':'explicit_pcm_format',
                              'source_sample_width_bits':width,'source_channels':channels,
                              'source_endian':endian,'source_signed':signed})
    result['limitations']=[x for x in result['limitations'] if '未与SIP/RTP/PCM' not in x]+[
        'Raw PCM 按用户明确提供的格式解释；参数错误会使波形和诊断候选失真。'
    ]
    return result


def analyze_field_audio(path: Path, *, ffmpeg_binary: str = 'ffmpeg', ffprobe_binary: str = 'ffprobe',
                        pcm_format: dict | None = None) -> dict:
    """Decode common field-audio containers to bounded PCM WAV, then run deterministic DSP."""
    if path.stat().st_size > 100 * 1024 * 1024:
        raise ValueError('AUDIO_FILE_TOO_LARGE')
    if path.suffix.lower() in {'.pcm','.raw'}:
        if not pcm_format: raise ValueError('RAW_PCM_FORMAT_REQUIRED')
        return analyze_raw_pcm(path,pcm_format)
    try:
        with path.open('rb') as fh:
            header=fh.read(12)
        if header.startswith(b'RIFF'):
            return analyze_field_wav(path)
    except ValueError:
        pass
    probe = subprocess.run(
        [ffprobe_binary, '-v', 'error', '-show_entries', 'format=duration,format_name',
         '-of', 'default=noprint_wrappers=1', str(path)],
        text=True, capture_output=True, timeout=20, check=False,
    )
    if probe.returncode != 0:
        raise ValueError('AUDIO_DECODE_UNAVAILABLE')
    metadata = dict(line.split('=', 1) for line in probe.stdout.splitlines() if '=' in line)
    try:
        duration = float(metadata.get('duration', '0'))
    except ValueError as exc:
        raise ValueError('AUDIO_DURATION_INVALID') from exc
    if duration <= 0 or duration > 600:
        raise ValueError('AUDIO_DURATION_INVALID')
    with tempfile.TemporaryDirectory(prefix='field-audio-decode-') as td:
        wav_path = Path(td) / 'decoded.wav'
        decode_field_audio_to_wav(path,wav_path,ffmpeg_binary=ffmpeg_binary)
        result = analyze_field_wav(wav_path)
    result['summary']['source_format'] = metadata.get('format_name', path.suffix.lstrip('.').lower() or 'unknown')
    result['summary']['decoded_by'] = 'ffmpeg'
    return result


def align_field_audio(field_wav: Path, media_tracks: list[dict], calls: list[dict]|None=None) -> dict:
    """Correlate a field recording against decoded RTP/PCM artifacts."""
    from app.analyzers.media.correlation import correlate_tracks
    field,field_rate=read_wav_mono(field_wav)
    alignments=[]
    for track in media_tracks:
        samples,rate=read_wav_mono(Path(track['path']))
        corr=correlate_tracks(field,field_rate,0.0,samples,rate,0.0,max_lag_ms=60000,max_seconds=60.0)
        if not corr or corr.get('quality')=='LOW': continue
        media_start=float(track.get('start_time') or 0.0)
        offset=media_start-float(corr['lag_ms'])/1000.0
        mapped_events=[]
        for event in track.get('field_events') or []:
            rel=float(event.get('time_seconds',event.get('start_seconds',0.0)))
            absolute=offset+rel
            call=next((c for c in calls or [] if float(c.get('media_start_time') or 1e30)<=absolute<=float(c.get('media_end_time') or -1e30)),None)
            mapped_events.append({'type':event.get('type'),'field_time_seconds':rel,'media_time':round(absolute,6),
                                  'call_id':call.get('call_id') if call else None})
        alignments.append({'source':track.get('source'),'stream_id':track.get('stream_id'),'pcm_tap':track.get('pcm_tap'),
                           'media_start_time':media_start,'field_zero_media_time':round(offset,6),
                           'correlation':corr,'mapped_events':mapped_events,'evidence_level':'L3'})
    alignments.sort(key=lambda x:x['correlation']['absolute_correlation'],reverse=True)
    return {'status':'SUCCESS','summary':{'availability':'ALIGNED' if alignments else 'NO_RELIABLE_MATCH',
            'alignment_count':len(alignments),'high_quality_count':sum(x['correlation']['quality']=='HIGH' for x in alignments)},
            'alignments':alignments[:10],
            'limitations':['相关对齐证明信号内容和时间偏移相似，但不能单独证明故障因果或具体硬件根因。']}
def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        pos += 2
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if pos + 2 > len(data):
            break
        length = struct.unpack('>H', data[pos:pos + 2])[0]
        if length < 2 or pos + length > len(data):
            break
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and length >= 7:
            height, width = struct.unpack('>HH', data[pos + 3:pos + 7])
            return width, height
        pos += length
    return None


def _redact_ocr_text(text: str) -> str:
    import re
    text = re.sub(r'(?im)^.*(?:password|passwd|pwd|token|secret|密码|口令).*$','[REDACTED_SENSITIVE_LINE]',text)
    return text[:4000]


def _structured_ocr_observations(text: str) -> list[dict]:
    import re
    specs = {
        'registration_status': r'(?:注册状态|registration\s*status)\s*[:：]?\s*([^\n]{1,40})',
        'codec': r'(?:编解码|codec)\s*[:：]?\s*([A-Za-z0-9_.-]{1,30})',
        'software_version': r'(?:软件版本|固件版本|version)\s*[:：]?\s*([A-Za-z0-9_.-]{1,60})',
        'alarm': r'(?:告警|alarm)\s*[:：]?\s*([^\n]{1,100})',
    }
    out=[]
    for key, pattern in specs.items():
        match=re.search(pattern,text,re.I)
        if match:
            out.append({'key':key,'value':match.group(1).strip(),'evidence_level':'L4','status':'OCR_CANDIDATE'})
    return out


def _visual_layout_candidates(path: Path, *, ffmpeg_binary: str = 'ffmpeg') -> dict:
    """Extract bounded geometry/color candidates without assigning UI meaning."""
    try:
        proc=subprocess.run([
            ffmpeg_binary,'-v','error','-nostdin','-i',str(path),'-vf','scale=128:128',
            '-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','pipe:1',
        ],capture_output=True,timeout=30,check=False)
    except (FileNotFoundError,subprocess.TimeoutExpired):
        return {'availability':'UNAVAILABLE','observations':[]}
    if proc.returncode!=0 or len(proc.stdout)!=128*128*3:
        return {'availability':'UNAVAILABLE','observations':[]}
    rgb=np.frombuffer(proc.stdout,dtype=np.uint8).reshape(128,128,3)
    gray=np.mean(rgb.astype(np.float32),axis=2)
    horizontal=float(np.mean(np.abs(np.diff(gray,axis=0))>=40))
    vertical=float(np.mean(np.abs(np.diff(gray,axis=1))>=40))
    quantized=(rgb//64).reshape(-1,3)
    colors,counts=np.unique(quantized,axis=0,return_counts=True)
    order=np.argsort(counts)[::-1][:5]
    dominant=[{'rgb_bucket':[int(x*64) for x in colors[i]],'ratio':round(float(counts[i]/len(quantized)),4)} for i in order]
    observations=[
        {'key':'horizontal_edge_density','value':round(horizontal,4),'evidence_level':'L4','status':'VISUAL_CANDIDATE'},
        {'key':'vertical_edge_density','value':round(vertical,4),'evidence_level':'L4','status':'VISUAL_CANDIDATE'},
        {'key':'dominant_color_buckets','value':dominant,'evidence_level':'L4','status':'VISUAL_CANDIDATE'},
    ]
    if horizontal>=0.02 and vertical>=0.02:
        observations.append({'key':'connected_layout','value':'POSSIBLE','evidence_level':'L4',
                             'status':'VISUAL_CANDIDATE'})
    return {'availability':'CANDIDATE_ONLY','width':128,'height':128,'observations':observations,
            'semantic_labels_assigned':False}


def inspect_image(path: Path, *, tesseract_binary: str = 'tesseract', ffmpeg_binary: str = 'ffmpeg') -> dict:
    data = path.read_bytes()
    image_format = None
    dimensions = None
    if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 24 and data[12:16] == b'IHDR':
        image_format = 'PNG'
        dimensions = struct.unpack('>II', data[16:24])
    elif data.startswith((b'GIF87a', b'GIF89a')) and len(data) >= 10:
        image_format = 'GIF'
        dimensions = struct.unpack('<HH', data[6:10])
    elif data.startswith(b'\xff\xd8'):
        image_format = 'JPEG'
        dimensions = _jpeg_size(data)
    elif data.startswith(b'RIFF') and len(data) >= 12 and data[8:12] == b'WEBP':
        image_format = 'WEBP'
    if not image_format:
        raise ValueError('IMAGE_FORMAT_UNSUPPORTED_OR_CORRUPT')
    summary = {'availability': 'METADATA_ONLY', 'format': image_format, 'size_bytes': len(data)}
    if dimensions:
        summary.update({'width': dimensions[0], 'height': dimensions[1]})
    ocr={'availability':'UNAVAILABLE','text':'','mean_confidence':None,'observations':[]}
    try:
        proc=subprocess.run([tesseract_binary,str(path),'stdout','-l','chi_sim+eng','--psm','6','tsv'],
                            text=True,capture_output=True,timeout=45,check=False)
        if proc.returncode==0:
            lines={}; confidences=[]
            for line in proc.stdout.splitlines()[1:]:
                cols=line.split('\t')
                if len(cols)>=12 and cols[11].strip():
                    key=tuple(cols[1:5])
                    lines.setdefault(key,[]).append(cols[11].strip())
                    try:
                        conf=float(cols[10])
                        if conf>=0: confidences.append(conf)
                    except ValueError: pass
            clean=_redact_ocr_text('\n'.join(' '.join(words) for words in lines.values()))
            ocr={'availability':'EXTRACTED' if clean else 'EMPTY','text':clean,
                 'mean_confidence':round(float(np.mean(confidences)),2) if confidences else None,
                 'observations':_structured_ocr_observations(clean)}
    except (FileNotFoundError,subprocess.TimeoutExpired):
        pass
    summary['ocr_availability']=ocr['availability']; summary['ocr_character_count']=len(ocr['text'])
    visual=_visual_layout_candidates(path,ffmpeg_binary=ffmpeg_binary)
    summary['visual_candidate_availability']=visual['availability']
    return {
        'status': 'PARTIAL_SUCCESS', 'summary': summary,
        'findings': [{'type': 'IMAGE_HEADER_VALID', 'evidence_level': 'L3', **summary}],
        'ocr':ocr,'visual_semantics':visual,
        'limitations': [
            'OCR文本和结构化字段均为L4候选，可能存在错字，必须与原始日志或设备状态交叉验证。',
            '几何、连线密度和颜色仅为L4视觉候选；系统不把颜色映射为告警，也不确认拓扑关系。',
        ],
    }
