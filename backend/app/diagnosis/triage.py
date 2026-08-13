from __future__ import annotations

def triage_summary(text:str) -> set[str]:
    t=(text or '').lower()
    out=set()
    groups={
        'AUDIO_NOISE':['电流音','杂音','噪音','噪声','蜂鸣','hum','noise'],
        'AUDIO_STUTTER':['卡顿','断音','断续','不连续','延迟','抖动','jitter','stutter'],
        'ECHO':['回声','回音','echo'],
        'ONE_WAY_AUDIO':['单通','单向无声','对方听不到','我听不到','one-way'],
        'NO_AUDIO':['无声','没声音','没有声音','no audio'],
        'DTMF':['丢号','dtmf','按键','拨号','号码'],
        'REGISTER':['注册不上','注册失败','未注册','register'],
        'CALL_SETUP':['呼叫失败','打不通','呼叫不通','invite'],
    }
    for name,words in groups.items():
        if any(w in t for w in words): out.add(name)
    return out
