from app.capture_v2.enums import RecoveryClassification
from app.capture_v2.producer.identity import ProducerIdentity
from app.capture_v2.recovery.classifier import classify_recovery
from app.capture_v2.recovery.models import ActiveEpochExpectation, RecoveryInventory


def _p(pid=10, *, epoch="CAP1", session="S1", legacy=False):
    path = "/tmp/aiVoip_ring_old/capture.pcap" if legacy else f"/tmp/aivoip_capture/epochs/{epoch}/active/capture.pcap"
    return ProducerIdentity(
        pid=pid,
        process_starttime=1000 + pid,
        cmdline=f"/usr/bin/tcpdump -ni br-lan_400 -w {path}",
        interface="br-lan_400",
        output_path=path,
        capture_epoch=None if legacy else epoch,
        session_id=None if legacy else session,
        legacy=legacy,
    )


def _active(pid=10, boot="BOOT1"):
    return ActiveEpochExpectation(
        epoch_id="E1",
        epoch_token="CAP1",
        boot_id=boot,
        producer_pid=pid,
        producer_starttime=1000 + pid,
    )


def test_clean_when_no_active_epoch_and_no_owned_process():
    inv = RecoveryInventory("BOOT1", None, None, None)
    d = classify_recovery(session_id="S1", inventory=inv, active=None)
    assert d.classification == RecoveryClassification.CLEAN


def test_same_session_alive_adopts_exact_identity():
    p = _p()
    inv = RecoveryInventory("BOOT1", 3, "S1", "BOOT1", v2_producers=(p,))
    d = classify_recovery(session_id="S1", inventory=inv, active=_active())
    assert d.classification == RecoveryClassification.SAME_SESSION_ALIVE
    assert d.current == p


def test_active_epoch_without_process_is_dead_not_clean():
    inv = RecoveryInventory("BOOT1", 3, "S1", "BOOT1")
    d = classify_recovery(session_id="S1", inventory=inv, active=_active())
    assert d.classification == RecoveryClassification.SAME_SESSION_DEAD


def test_multiple_producers_keeps_unique_exact_current_and_marks_legacy_stale():
    current = _p(pid=10)
    legacy = _p(pid=20, legacy=True)
    inv = RecoveryInventory("BOOT1", 4, "S1", "BOOT1", v2_producers=(current,), legacy_producers=(legacy,))
    d = classify_recovery(session_id="S1", inventory=inv, active=_active(pid=10))
    assert d.classification == RecoveryClassification.MULTIPLE_PRODUCERS
    assert d.current == current
    assert d.stale == (legacy,)


def test_boot_id_change_has_priority_over_process_count():
    p = _p()
    inv = RecoveryInventory("BOOT2", 4, "S1", "BOOT2", v2_producers=(p,))
    d = classify_recovery(session_id="S1", inventory=inv, active=_active(boot="BOOT1"))
    assert d.classification == RecoveryClassification.DUT_REBOOT
