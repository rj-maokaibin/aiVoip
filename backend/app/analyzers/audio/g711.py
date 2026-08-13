from __future__ import annotations
import array


def _alaw_sample(a: int) -> int:
    a ^= 0x55
    t = (a & 0x0F) << 4
    seg = (a & 0x70) >> 4
    if seg == 0:
        t += 8
    elif seg == 1:
        t += 0x108
    else:
        t += 0x108
        t <<= seg - 1
    return t if (a & 0x80) else -t


def _mulaw_sample(u: int) -> int:
    u = (~u) & 0xFF
    t = ((u & 0x0F) << 3) + 0x84
    t <<= (u & 0x70) >> 4
    return (0x84 - t) if (u & 0x80) else (t - 0x84)


def decode_alaw(payload: bytes) -> bytes:
    samples = array.array('h', (_alaw_sample(b) for b in payload))
    if samples.itemsize != 2:
        raise RuntimeError('UNSUPPORTED_HOST_SHORT_SIZE')
    if __import__('sys').byteorder != 'little':
        samples.byteswap()
    return samples.tobytes()


def decode_mulaw(payload: bytes) -> bytes:
    samples = array.array('h', (_mulaw_sample(b) for b in payload))
    if samples.itemsize != 2:
        raise RuntimeError('UNSUPPORTED_HOST_SHORT_SIZE')
    if __import__('sys').byteorder != 'little':
        samples.byteswap()
    return samples.tobytes()
