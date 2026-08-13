import struct
from app.analyzers.audio.g711 import decode_alaw, decode_mulaw

def test_alaw_common_silence_code_decodes_near_zero():
    vals=struct.unpack('<4h', decode_alaw(bytes([0xD5]*4)))
    assert vals==(8,8,8,8)

def test_g711_decoder_preserves_sample_count():
    assert len(decode_alaw(bytes(range(256))))==512
    assert len(decode_mulaw(bytes(range(256))))==512
