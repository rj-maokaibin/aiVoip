"""Validate the updated RUIJIE_VOIP_AIM_V1 platform profile loads through the registry."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.platforms.registry import PlatformProfileRegistry


def main():
    reg = PlatformProfileRegistry()
    loaded = reg.get('RUIJIE_VOIP_AIM_V1')
    p = loaded.definition
    print('profile loaded OK:', p.id, p.version, p.status.value)
    print('gaps:', [g.key for g in p.gaps])
    print('templates:')
    for t in p.known_diagnostic_templates:
        extra = ''
        if t.submode_prompt:
            extra = f" submode={t.submode_prompt!r} snapshot={t.snapshot_command!r}"
        print('  -', t.template_id, f'[{t.status}]', extra)


if __name__ == '__main__':
    main()
