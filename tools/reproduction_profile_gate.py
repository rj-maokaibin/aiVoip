#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))

from app.reproduction.profile import ReproductionProfileRegistry

EXPECTED={'REGISTER_FAILURE','CALL_SETUP_FAILURE','ONE_WAY_AUDIO','AUDIO_STUTTER','AUDIO_NOISE','DTMF_LOSS','ECHO','VOIP_GENERIC_FULL_CAPTURE'}
registry=ReproductionProfileRegistry(ROOT/'profiles')
loaded=registry.list(); ids={x.definition.id for x in loaded}
assert ids==EXPECTED,(ids,EXPECTED)
for item in loaded:
    d=item.definition
    assert d.version and len(item.checksum)==64
    assert d.cleanup_actions
    assert any(s.stage.value=='BASE' for s in d.stages)
print(json.dumps({'status':'PASS','profiles':len(loaded),'ids':sorted(ids)},ensure_ascii=False))
