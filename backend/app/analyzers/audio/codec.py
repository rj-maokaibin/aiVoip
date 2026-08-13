from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
import numpy as np

from .g711 import decode_alaw, decode_mulaw


@dataclass(frozen=True, slots=True)
class CodecSpec:
    name: str
    sample_rate: int
    channels: int
    decoder: Callable[[bytes], bytes]

    def decode_i16(self, payload: bytes) -> np.ndarray:
        data = self.decoder(payload)
        return np.frombuffer(data, dtype='<i2').copy()


_REGISTRY: dict[str, CodecSpec] = {
    'PCMA': CodecSpec('PCMA', 8000, 1, decode_alaw),
    'G711A': CodecSpec('PCMA', 8000, 1, decode_alaw),
    'ALAW': CodecSpec('PCMA', 8000, 1, decode_alaw),
    'PCMU': CodecSpec('PCMU', 8000, 1, decode_mulaw),
    'G711U': CodecSpec('PCMU', 8000, 1, decode_mulaw),
    'MULAW': CodecSpec('PCMU', 8000, 1, decode_mulaw),
}


def get_codec(name: str | None) -> CodecSpec | None:
    if not name:
        return None
    return _REGISTRY.get(name.upper())


def supported_codecs() -> list[str]:
    return sorted({spec.name for spec in _REGISTRY.values()})
