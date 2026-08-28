from __future__ import annotations

import asyncio

from app.capture_v2.transport.readonly import ReadOnlyDeviceTransport


class _Result:
    def __init__(self, stdout="", exit_status=0, stderr=""):
        self.stdout = stdout
        self.exit_status = exit_status
        self.stderr = stderr


class _CapturingAdapter:
    def __init__(self, output=""):
        self.output = output
        self.commands = []

    async def execute_shell(self, command, timeout=None, retries=None):
        self.commands.append(command)
        return _Result(stdout=self.output)


def _detect(proc_table, command):
    """Replicate the shell matching logic in Python.

    proc_table maps pid -> (comm, cmdline).
    """
    out_lines = []
    for pid, (comm, cmdline) in proc_table.items():
        if comm not in ("tcpdump", "tshark"):
            continue
        out_lines.append("%s\t123\t%s" % (pid, cmdline))
    return out_lines


def test_list_tcpdump_command_uses_comm_not_cmdline_substring():
    """Regression: the scan must match by /proc/PID/comm (process name), not by
    cmdline substring. The old `case \"$cmd\" in *tcpdump*)` matched the scanning
    shell's own cmdline (it literally contains the pattern), producing a false
    SIP_ABA_EXISTING_TCPDUMP_PRESENT on every live gate run."""
    adapter = _CapturingAdapter()
    reader = ReadOnlyDeviceTransport(adapter)
    asyncio.run(reader.list_tcpdump_processes())
    assert len(adapter.commands) == 1
    command = adapter.commands[0]

    # fix present: comm-based matching, tcpdump|tshark names
    assert "$p/comm" in command
    assert 'case "$c" in tcpdump|tshark' in command
    # the buggy self-matching cmdline substring check must be gone
    assert 'case "$cmd" in *tcpdump*)' not in command


def test_tcpdump_detection_ignores_scanner_self_match():
    """The comm-based semantics must detect a real tcpdump but ignore an ash
    process whose cmdline merely mentions tcpdump (the scanner itself)."""
    proc_table = {
        101: ("tcpdump", "/usr/sbin/tcpdump -i br-lan_400 -s 0 -U -w /tmp/a.pcap"),
        102: ("ash", 'ash -c for p in /proc/[0-9]*; do case "$cmd" in *tcpdump*) ;; esac; done'),
        103: ("tshark", "/usr/bin/tshark -i br-lan_400"),
    }
    command = r'''for p in /proc/[0-9]*; do
  [ -r "$p/comm" ] || continue
  c=$(cat "$p/comm" 2>/dev/null)
  case "$c" in tcpdump|tshark) ;; *) continue ;; esac
  [ -r "$p/cmdline" ] || continue
  cmd=$(tr '\000' ' ' < "$p/cmdline" 2>/dev/null)
  st=$(awk '{print $22}' "$p/stat" 2>/dev/null) || continue
  printf '%s\t%s\t%s\n' "${p##*/}" "$st" "$cmd"
done'''
    detected = _detect(proc_table, command)
    pids = sorted(int(line.split("\t")[0]) for line in detected)
    # only the real tcpdump and tshark are reported; the ash scanner is not
    assert pids == [101, 103]

    # demonstrate the old buggy logic would have wrongly included the scanner
    buggy = [pid for pid, (comm, cmd) in proc_table.items() if "tcpdump" in cmd]
    assert 102 in buggy
