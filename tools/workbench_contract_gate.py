#!/usr/bin/env python3
from pathlib import Path
import json
import sys

ROOT=Path(__file__).resolve().parents[1]
front=(ROOT/'frontend/src/main.tsx').read_text(encoding='utf-8')
repro=(ROOT/'backend/app/api/v1/reproduction.py').read_text(encoding='utf-8')
exp=(ROOT/'backend/app/api/v1/experiments.py').read_text(encoding='utf-8')
feishu=(ROOT/'backend/app/integrations/feishu/cards.py').read_text(encoding='utf-8')

checks={
 'tabs': all(x in front for x in ['总览','诊断','自动复现','实验/因果','Evidence','飞书卡片','审计']),
 'packet_media_views': all(x in front for x in ['Packet Intelligence','Media Intelligence','PCM ↔ RTP 自动关联','Unified Timeline','Waveform','Spectrogram']),
 'sse': '/events/stream?case_id=' in front and 'TARGET_CONFIRMED' in front and 'CLEANUP_ALERT' in front,
 'safe_stop': 'Finalize → Cleanup' in front and '/stop' in front,
 'ec02_pending_banner': 'EC-02' in front and '真实 DUT 命令不会被猜测执行' in front,
 'case_reproduction_list_api': "'/cases/{case_id}/reproductions'" in repro,
 'case_experiment_list_api': "'/cases/{case_id}/experiments'" in exp,
 'case_fix_read_api': "'/cases/{case_id}/fix-actions'" in exp and "'/cases/{case_id}/fix-verifications'" in exp,
 'single_feishu_card': 'FeishuCaseCardBuilder' in feishu and '停止自动复现' in feishu and '已完成操作' in feishu,
 'feishu_transport_separated_from_card_builder': 'httpx' not in feishu and 'webhook' not in feishu.lower(),
 'no_real_dut_commands_in_workbench': all(x not in front+feishu for x in ['voip dsp diag set','debug p on','de sip de','reboot -f','sysupgrade']),
}
passed=all(checks.values())
print(json.dumps({'status':'PASS' if passed else 'FAIL','checks':checks},ensure_ascii=False,indent=2))
sys.exit(0 if passed else 1)
