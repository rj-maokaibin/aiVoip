from app.reproduction.pcm_cleanup import (
    BusyboxTcpdumpPcmProbe,
    PcmCleanupGuard,
    build_busybox_tcpdump_probe,
    parse_tcpdump_packet_count,
)


def _guard(*, probes, commands):
    return PcmCleanupGuard(
        probe_packets=lambda interface, port: probes.pop(0),
        execute_aim=commands.append,
    )


def test_quiet_pcm_skips_non_idempotent_off_command():
    commands = []
    result = _guard(probes=[0], commands=commands).cleanup_channel(
        channel='PCM_RX', voice_interface='br-lan_400', voice_gateway_ip='192.168.3.200'
    )

    assert commands == []
    assert result.quiet_verified is True
    assert result.off_executed is False


def test_active_pcm_executes_off_once_then_verifies_quiet():
    commands = []
    result = _guard(probes=[3, 0], commands=commands).cleanup_channel(
        channel='PCM_TX', voice_interface='br-lan_400', voice_gateway_ip='192.168.3.200'
    )

    assert commands == ['voip dsp diag set 192.168.3.200 50000 1 pcm_tx off']
    assert result.off_executed is True
    assert result.quiet_verified is True


def test_retry_never_sends_a_second_off_when_previous_off_did_not_quiet_stream():
    commands = []
    first = _guard(probes=[2, 1], commands=commands).cleanup_channel(
        channel='PCM_RX', voice_interface='br-lan_400', voice_gateway_ip='192.168.3.200'
    )
    retry = _guard(probes=[1], commands=commands).cleanup_channel(
        channel='PCM_RX', voice_interface='br-lan_400', voice_gateway_ip='192.168.3.200',
        off_already_executed=first.off_executed,
    )

    assert commands == ['voip dsp diag set 192.168.3.200 40000 1 pcm_rx off']
    assert first.quiet_verified is False
    assert retry.retry_blocked is True
    assert retry.quiet_verified is False


def test_busybox_probe_uses_device_timeout_syntax_and_parses_zero_packets():
    command = build_busybox_tcpdump_probe(voice_interface='br-lan_400', port=40000)
    output = 'listening on br-lan_400\n\n0 packets captured\n0 packets received by filter\n'

    assert command == "timeout -t 5 tcpdump -ni br-lan_400 -c 1 'udp port 40000' 2>&1"
    assert BusyboxTcpdumpPcmProbe(execute_shell=lambda _: output)('br-lan_400', 40000) == 0


def test_parse_accepts_singular_packet_captured_from_active_stream():
    # Real BusyBox output for a single captured packet uses the singular form and also
    # reports 'received by filter' lines; the captured count is the authoritative one.
    output = (
        'tcpdump: verbose output suppressed\n'
        'listening on br-lan_400, link-type EN10MB\n'
        '21:57:34.758955 IP 192.168.150.4.42569 > 192.168.3.200.40000: UDP, length 160\n'
        '1 packet captured\n'
        '99 packets received by filter\n'
        '0 packets dropped by kernel\n'
    )
    assert parse_tcpdump_packet_count(output) == 1


def test_tcpdump_probe_rejects_output_without_a_capture_count():
    try:
        parse_tcpdump_packet_count('tcpdump: permission denied')
    except ValueError as exc:
        assert str(exc) == 'PCM_TCPDUMP_CAPTURE_COUNT_MISSING'
    else:
        raise AssertionError('missing tcpdump packet count must be rejected')