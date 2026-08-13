import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from app.actions.registry import ActionRegistry
from app.analyzers.profile import load_analyzer_profile
from app.analyzers.pcm.profile import load_pcm_profile
from app.platforms.registry import PlatformProfileRegistry

r=ActionRegistry(ROOT/'profiles')
print(f'actions={len(r.actions)} profiles={len(r.profiles)}')
for pid,p in r.profiles.items():
    print(pid, '->', ', '.join(p.actions))

analyzer=load_analyzer_profile(ROOT/'profiles/analyzers/voip_v1.yaml')
print('analyzer_profile', analyzer.id, analyzer.version, analyzer.status, analyzer.checksum)

for pcm_path in sorted((ROOT/'profiles/pcm').glob('*.yaml')):
    pcm=load_pcm_profile(pcm_path)
    print('pcm_profile', pcm.id, pcm.version, pcm.format_status, pcm.checksum)

platform_registry=PlatformProfileRegistry(ROOT/'profiles')
for loaded in platform_registry.list():
    p=loaded.definition
    print('platform_profile', p.id, p.version, p.status.value, loaded.checksum, 'gaps=', len(p.gaps))
