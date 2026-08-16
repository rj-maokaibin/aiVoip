"""Recompute real-phone (RP) case status from the reproduction DB.

The ledger in docs/真机用例执行台账.md was hand-built by reading the DB once.
This tool makes it reproducible: it derives per-session facts, evaluates the
four acceptance dimensions, and reports each RP id's status.

Design constraint that shapes everything below: a session row does not record
what the operator *intended* to do. "Speak 5s, mute 5s, repeat" and "talk for
30s" are indistinguishable in the schema. So this tool never guesses intent.

  * Sessions are bound to an RP id only through an explicit label registry
    (docs/真机用例执行台账.labels.json). A labelled session is auto-judged
    against that id's declared expectations.
  * Unlabelled sessions still contribute to CROSS-CUTTING facts (which DTMF
    digits have ever been captured, whether any in-call DTMF event exists,
    residual ACTIVE rows). These are matrix-wide gaps, not per-case verdicts.
  * Ids with no label and no cross-cutting evidence stay NOT_RUN. The tool
    will not promote a case to PASS on circumstantial data.

Usage (from the repo root, needs DB reachable):
    PYTHONPATH=backend python tools/dev_case_ledger.py            # report
    PYTHONPATH=backend python tools/dev_case_ledger.py --json out.json
    PYTHONPATH=backend python tools/dev_case_ledger.py --label <sid>=RP-R01

Label a session as it is run, then rerun to refresh the verdict.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

LABELS_PATH = ROOT / "docs" / "真机用例执行台账.labels.json"

# --- matrix definition -------------------------------------------------------
# expect keys are optional; only declared ones are enforced.
#   dtmf   : exact digit string expected from FXS events
#   call   : True -> exactly one call, False -> no call must be created
#   hook_s : (min, max) inclusive bounds on OFFHOOK->ONHOOK seconds
#   manual : dimension cannot be settled from DB alone (needs operator note)
CASE_SPEC: dict[str, dict] = {}


def _c(cid, title, **expect):
    CASE_SPEC[cid] = {"title": title, "expect": expect}


# 一、基础可靠性
_c("RP-R01", "等30s摘机拨301通话10s挂机", dtmf="301", call=True)
_c("RP-R02", "摘机不拨号等5s挂机", dtmf="", call=False, hook_s=(3, 8))
_c("RP-R03", "摘机1s内立即挂机", dtmf="", call=False, hook_s=(0, 1.5))
_c("RP-R04", "拨完301接通前立即挂机", dtmf="301", call=False)
_c("RP-R05", "接通后1-2s立即挂机", dtmf="301", call=True, hook_s=(0, 12))
_c("RP-R06", "接通后通话30s", dtmf="301", call=True, hook_s=(25, 45))
_c("RP-R07", "通话3-5分钟", dtmf="301", call=True, hook_s=(170, 320))
_c("RP-R08", "连续10个独立Session", manual=True)
_c("RP-R09", "上轮完成后立即启动下一轮", manual=True)
_c("RP-R10", "WATCHING保持3-5分钟后再打", call=True, manual=True)
# 二、摘挂机与状态机边界
_c("RP-H01", "快速摘挂机连续5次", manual=True)
_c("RP-H02", "挂机后0.5s再摘机", manual=True)
_c("RP-H03", "摘机停20s不拨号再挂机", dtmf="", call=False, hook_s=(15, 28))
_c("RP-H04", "拨号中挂机(只拨30)", dtmf="30", call=False)
_c("RP-H05", "对端挂机后本端延迟5s挂机", call=True, manual=True)
_c("RP-H06", "对端先挂机后本端立即再拨", manual=True)
_c("RP-H07", "振铃阶段反复按叉簧", manual=True)
_c("RP-H08", "通话结束瞬间按一个DTMF再挂机", call=True, manual=True)
# 三、拨号与 DTMF
_c("RP-D01", "正常速度拨301", dtmf="301", call=True)
_c("RP-D02", "慢拨3/停2s/0/停2s/1", dtmf="301", call=True)
_c("RP-D03", "快速拨301", dtmf="301", call=True)
_c("RP-D04", "拨301#", dtmf="301#")
_c("RP-D05", "拨*301#", dtmf="*301#")
_c("RP-D06", "拨重复数字11110000", dtmf="11110000")
_c("RP-D07", "拨0123456789", dtmf="0123456789")
_c("RP-D08", "通话中按123#789", call=True, in_call_dtmf=True)
_c("RP-D09", "长按某数字2秒", manual=True)
_c("RP-D10", "拨不存在的号码", call=False, manual=True)
# 四、Call Binding
_c("RP-B01", "摘机停在拨号音不拨号", dtmf="", call=False)
_c("RP-B02", "正常拨301优先SIP_INVITE绑定", dtmf="301", call=True, bind="SIP_INVITE")
_c("RP-B03", "SIP不可见RTP正常->FALLBACK", call=True, bind="RTP_STREAM_START_FALLBACK")
_c("RP-B04", "挂机前刚出现SIP/RTP", manual=True)
_c("RP-B05", "仅零散UDP/伪RTP包", call=False, manual=True)
_c("RP-B06", "双向多个RTP SSRC", call=True, manual=True)
_c("RP-B07", "呼叫失败无RTP", call=False, manual=True)
_c("RP-B08", "BYE/ONHOOK接近Segment边界", call=True, manual=True)
# 五、连续采集与媒体稳定性
_c("RP-C01", "通话持续说话30s", call=True, hook_s=(25, 45))
_c("RP-C02", "连续说话3分钟", call=True, hook_s=(170, 320))
_c("RP-C03", "说话5s静音5s x3", call=True, manual=True)
_c("RP-C04", "通话中静音键10s", call=True, manual=True)
_c("RP-C05", "遮住本端麦克风10s", call=True, manual=True)
_c("RP-C06", "对端静音本端持续说话", call=True, manual=True)
_c("RP-C07", "双方同时说话15s", call=True, manual=True)
_c("RP-C08", "通话中切换免提/听筒", call=True, manual=True)
_c("RP-C09", "Segment边界附近挂机", call=True, manual=True)
_c("RP-C10", "空闲2分钟后开始通话", call=True, manual=True)
# 六、正常通话的误报检查
_c("RP-A01", "正常通话15s不得因DTMF_PATH判故障", call=True, no_fault=True)
_c("RP-A02", "不得因ECHO_PATH判回声故障", call=True, no_fault=True)
_c("RP-A03", "短暂停顿不得诊断卡顿", call=True, no_fault=True, manual=True)
_c("RP-A04", "HIGH hum但用户未听到噪声", manual=True)
_c("RP-A05", "单方向暂时静音不得诊断单通", call=True, no_fault=True, manual=True)
_c("RP-A06", "免提Click/Pop只能作候选", call=True, no_fault=True, manual=True)
_c("RP-A07", "正常通话Generic Profile应INCONCLUSIVE", call=True, verdict="INCONCLUSIVE")
_c("RP-A08", "不得无依据出现SUPPORTED/CONFIRMED", call=True, no_fault=True)
# 七、异常场景（话机辅助）
for _i, _t in enumerate(
    [
        "拨不存在的号码",
        "拨忙线号码",
        "呼叫不接听等超时",
        "振铃中挂机取消",
        "免提制造声学回授",
        "通话中反复切换静音键",
        "快速拨长号码",
        "慢拨并中途改号",
    ],
    start=1,
):
    _c(f"RP-F{_i:02d}", _t, manual=True)
# 八、Cleanup 与恢复安全（session 级安全维度）
_c("RP-S01", "PCM Cleanup 40000/50000 quiet verified", safety="pcm_quiet")
_c("RP-S02", "Debug Cleanup 已关闭", safety="debug_off")
_c("RP-S03", "Ring Cleanup DUT无残留producer", safety="pcap_closed", manual=True)
_c("RP-S04", "Lock Cleanup 已释放", safety="lock_released")
_c("RP-S05", "Evidence Finalize Manifest+checksum", safety="finalize_ok")
_c("RP-S06", "Worker Cleanup 无残留CAPTURING", safety="no_residual")
_c("RP-S07", "Cancel测试 WATCHING时取消立即执行", manual=True)
_c("RP-S08", "Recovery测试 人为中断watcher", manual=True)
_c("RP-S09", "Cleanup后能立即创建新Session", safety="next_round", manual=True)
_c("RP-S10", "Cleanup metadata mock_platform=false", safety="real_platform")

SECTIONS = [
    ("一、基础可靠性 R", "RP-R"),
    ("二、摘挂机与状态机边界 H", "RP-H"),
    ("三、拨号与 DTMF 识别准确性 D", "RP-D"),
    ("四、Call Binding 准确性 B", "RP-B"),
    ("五、连续采集与媒体稳定性 C", "RP-C"),
    ("六、正常通话的误报检查 A", "RP-A"),
    ("七、可由话机辅助制造的异常场景 F", "RP-F"),
    ("八、Cleanup 与恢复安全 S", "RP-S"),
]


# --- fact extraction ---------------------------------------------------------
def collect(db):
    from app.db.models import (
        CleanupRun,
        DeviceDiagnosticLock,
        EvidenceFinalizeRun,
        ReproductionAttempt,
        ReproductionCall,
        ReproductionCaptureSegment,
        ReproductionEventRecord,
        ReproductionSession,
    )
    from sqlalchemy import select

    sessions = {s.id: s for s in db.execute(select(ReproductionSession)).scalars()}
    facts: dict[str, dict] = {
        sid: {
            "state": s.state,
            "created_at": s.created_at,
            "digits": "",
            "hook_src": set(),
            "bind": set(),
            "in_call_dtmf": 0,
            "hook_s": None,
            "calls": 0,
            "verdicts": set(),
            "findings": set(),
            "attempts": [],
            "segments": 0,
            "retained": 0,
            "evicted": 0,
            "cleanup": None,
            "finalize": None,
            "mock_platform": None,
            "pcm_quiet": None,
            "debug_off": None,
            "pcap_closed": None,
        }
        for sid, s in sessions.items()
    }

    dtmf_by_sid = defaultdict(list)
    off_ms, on_ms = {}, {}
    for e in db.execute(select(ReproductionEventRecord)).scalars():
        f = facts.get(e.session_id)
        if f is None:
            continue
        if e.event_type == "FXS_DTMF":
            d = (e.payload_json or {}).get("digit")
            dtmf_by_sid[e.session_id].append((e.session_relative_ms or 0, str(d or "?")))
            if e.call_id:
                f["in_call_dtmf"] += 1
        elif e.event_type in ("FXS_OFFHOOK", "FXS_ONHOOK"):
            f["hook_src"].add(e.source)
            tgt = off_ms if e.event_type == "FXS_OFFHOOK" else on_ms
            ms = e.session_relative_ms
            if ms is not None:
                cur = tgt.get(e.session_id)
                tgt[e.session_id] = ms if cur is None else (min(cur, ms) if tgt is off_ms else max(cur, ms))
        elif e.event_type in ("SIP_INVITE", "RTP_STREAM_START", "RTP_STREAM_START_FALLBACK"):
            f["bind"].add(e.event_type)

    for sid, seq in dtmf_by_sid.items():
        facts[sid]["digits"] = "".join(d for _, d in sorted(seq))
    for sid in facts:
        a, b = off_ms.get(sid), on_ms.get(sid)
        if a is not None and b is not None and b >= a:
            facts[sid]["hook_s"] = round((b - a) / 1000.0, 1)

    for a in db.execute(select(ReproductionAttempt)).scalars():
        if a.session_id in facts:
            facts[a.session_id]["attempts"].append(a.status)
    for c in db.execute(select(ReproductionCall)).scalars():
        f = facts.get(c.session_id)
        if f is None:
            continue
        f["calls"] += 1
        if c.verdict:
            f["verdicts"].add(c.verdict)
        for fd in (c.quick_analysis_json or {}).get("findings", []) or []:
            f["findings"].add(fd)
    for g in db.execute(select(ReproductionCaptureSegment)).scalars():
        f = facts.get(g.session_id)
        if f is None:
            continue
        f["segments"] += 1
        if g.status == "EVICTED":
            f["evicted"] += 1
        elif g.retained:
            f["retained"] += 1
    for cr in db.execute(select(CleanupRun)).scalars():
        f = facts.get(cr.session_id)
        if f is None:
            continue
        f["cleanup"] = cr.status
        v = cr.validation_json or {}
        fin = v.get("final") or {}
        f["pcm_quiet"] = bool(
            (fin.get("PCM_RX") or {}).get("quiet_verified") and (fin.get("PCM_TX") or {}).get("quiet_verified")
        )
        f["debug_off"] = bool((fin.get("DEBUG") or {}).get("off_verified"))
        f["pcap_closed"] = bool((fin.get("PCAP") or {}).get("closed_verified"))
        ar = cr.action_results_json or {}
        if "mock_platform" in ar:
            f["mock_platform"] = ar["mock_platform"]
    for fr in db.execute(select(EvidenceFinalizeRun)).scalars():
        f = facts.get(fr.session_id)
        if f is not None:
            f["finalize"] = (fr.status, bool(fr.manifest_sha256))

    locks = defaultdict(set)
    for lk in db.execute(select(DeviceDiagnosticLock)).scalars():
        locks[lk.session_id].add(lk.status)

    residual = {
        "active_attempts": sum(1 for f in facts.values() for st in f["attempts"] if st == "ACTIVE"),
        "active_calls": db.execute(
            select(ReproductionCall).where(ReproductionCall.status == "ACTIVE")
        ).scalars().all().__len__(),
        "open_sessions": sum(1 for f in facts.values() if f["state"] in ("WATCHING", "CAPTURING", "ARMING")),
    }
    cross = {
        "digits_seen": sorted({c for f in facts.values() for c in f["digits"] if c != "?"}),
        "in_call_dtmf_total": sum(f["in_call_dtmf"] for f in facts.values()),
        "last_session_at": max((f["created_at"] for f in facts.values()), default=None),
        "residual": residual,
        "lock_statuses": sorted({s for v in locks.values() for s in v}),
    }
    return facts, cross


# --- per-case evaluation -----------------------------------------------------
def judge(cid: str, spec: dict, f: dict) -> tuple[str, list[str]]:
    """Return (verdict, reasons) for one labelled session against one case."""
    exp = spec["expect"]
    bad: list[str] = []

    # 动作
    if f["hook_src"] and f["hook_src"] != {"REAL_PLATFORM"}:
        bad.append(f"hook source not REAL_PLATFORM: {sorted(f['hook_src'])}")
    if "dtmf" in exp and f["digits"] != exp["dtmf"]:
        bad.append(f"dtmf {f['digits']!r} != expected {exp['dtmf']!r}")
    if "call" in exp:
        if exp["call"] and f["calls"] != 1:
            bad.append(f"expected exactly 1 call, got {f['calls']}")
        if not exp["call"] and f["calls"] != 0:
            bad.append(f"expected no call, got {f['calls']}")
    if "hook_s" in exp and f["hook_s"] is not None:
        lo, hi = exp["hook_s"]
        if not (lo <= f["hook_s"] <= hi):
            bad.append(f"hook duration {f['hook_s']}s outside [{lo},{hi}]")
    if "bind" in exp and exp["bind"] not in f["bind"]:
        bad.append(f"missing bind event {exp['bind']} (have {sorted(f['bind']) or 'none'})")
    if exp.get("in_call_dtmf") and f["in_call_dtmf"] == 0:
        bad.append("no in-call DTMF event (call_id IS NULL on all FXS_DTMF)")

    # 诊断
    if "verdict" in exp and f["verdicts"] and exp["verdict"] not in f["verdicts"]:
        bad.append(f"verdict {sorted(f['verdicts'])} != expected {exp['verdict']}")
    if exp.get("no_fault") and f["verdicts"] & {"MATCH", "TARGET"}:
        bad.append(f"normal call must not yield {sorted(f['verdicts'] & {'MATCH','TARGET'})}")

    # 安全（对所有已标注 session 一律强制）
    if f["cleanup"] != "VERIFIED":
        bad.append(f"cleanup={f['cleanup']}")
    if f["mock_platform"] is not False:
        bad.append(f"mock_platform={f['mock_platform']}")
    if f["finalize"] and f["finalize"] != ("SUCCESS", True):
        bad.append(f"finalize={f['finalize']}")
    if "ACTIVE" in f["attempts"]:
        bad.append("residual ACTIVE attempt")

    if bad:
        return "FAIL", bad
    return ("PASS_NEEDS_NOTE", ["auto checks pass; operator note required for intent"]) if exp.get("manual") else ("PASS", [])


def build(facts, cross, labels):
    by_case = defaultdict(list)
    for sid, cid in labels.items():
        if sid in facts:
            by_case[cid].append(sid)

    rows = []
    for cid, spec in CASE_SPEC.items():
        sids = by_case.get(cid, [])
        if not sids:
            rows.append({"id": cid, "title": spec["title"], "status": "NOT_RUN", "sessions": [], "reasons": []})
            continue
        results = [(sid, *judge(cid, spec, facts[sid])) for sid in sids]
        if any(r[1] == "PASS" for r in results):
            st = "PASS"
        elif any(r[1] == "PASS_NEEDS_NOTE" for r in results):
            st = "PASS_NEEDS_NOTE"
        else:
            st = "FAIL"
        rows.append(
            {
                "id": cid,
                "title": spec["title"],
                "status": st,
                "sessions": [r[0] for r in results],
                "reasons": [f"{r[0][:8]}: {'; '.join(r[2])}" for r in results if r[2]],
            }
        )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--label", action="append", default=[], metavar="SID=RP-ID")
    args = ap.parse_args()

    labels = {}
    if LABELS_PATH.exists():
        labels = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    for spec in args.label:
        sid, _, cid = spec.partition("=")
        if cid not in CASE_SPEC:
            print(f"unknown case id: {cid}", file=sys.stderr)
            return 2
        labels[sid] = cid
    if args.label:
        LABELS_PATH.write_text(json.dumps(labels, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"labels written: {LABELS_PATH.relative_to(ROOT)} ({len(labels)} entries)")

    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        facts, cross = collect(db)
    finally:
        db.close()

    rows = build(facts, cross, labels)
    tally = defaultdict(int)
    for r in rows:
        tally[r["status"]] += 1

    print(f"\nRP case ledger — {len(rows)} ids, {len(labels)} labelled session(s)")
    print(f"  sessions in DB : {len(facts)}   last: {cross['last_session_at']}")
    print(f"  DTMF digits    : {cross['digits_seen'] or 'none'}")
    print(f"  in-call DTMF   : {cross['in_call_dtmf_total']}")
    print(f"  residual       : {cross['residual']}")
    print(f"  lock statuses  : {cross['lock_statuses']}")
    print("  tally          : " + ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))

    for title, prefix in SECTIONS:
        sec = [r for r in rows if r["id"].startswith(prefix)]
        print(f"\n{title}")
        for r in sec:
            mark = {"PASS": "PASS", "PASS_NEEDS_NOTE": "PASS*", "FAIL": "FAIL", "NOT_RUN": "  - "}[r["status"]]
            sfx = f"  [{','.join(s[:8] for s in r['sessions'])}]" if r["sessions"] else ""
            print(f"  {mark:5} {r['id']}  {r['title']}{sfx}")
            for why in r["reasons"]:
                print(f"           ! {why}")

    print("\nPASS* = automated judges pass but the case encodes operator intent")
    print("        the schema cannot confirm; attach a note before accepting.")

    if args.json_out:
        payload = {
            "cross_cutting": {**cross, "last_session_at": str(cross["last_session_at"])},
            "tally": dict(tally),
            "cases": rows,
        }
        Path(args.json_out).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\njson written: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
