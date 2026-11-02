#!/usr/bin/env python3
r"""
capture.py -- boot-time capture + log parsing for an already-flashed
Qualcomm target device.

This is the entry point for the `bootbench` skill's capture stage (see
`../../SKILL.md`): boot an already-flashed device N times under the
default kernel cmdline, then N more with debug cmdline params added, pull
all collected logs off the device via `adb`, parse them into structured
metrics, run outlier/regression/root-cause analysis, and record + render
the report (via report.py).

This module is fully standalone -- it has NO dependency on any flashing
tool. There is no PCAT/EDL step, and no nightly-build-share discovery:
flashing a device with a new build is out of scope for this skill, which
assumes the device already has the build under test. Each capture simply
labels the build it ran against "Build-1", "Build-2", ... auto-incrementing
per target based on what's already recorded in that target's
bootchart-data-<slug>.json (see next_build_label()) -- unless --build-path
is given explicitly, in which case that label is used verbatim instead.

Two transports are supported, chosen by the CLI args given:

- Serial console (default): a serial (COM) console is used for login and
  for injecting debug cmdline params, and an Alpaca TAC debug board is
  used to power-cycle the device between boots (there is no other
  primitive here to force a clean reboot) -- boot/reboot control only,
  not flashing. Logs are pulled off the device via `adb`.
- SSH (--host): the device is reached over SSH instead of a serial
  console, and power-cycled between boots with a plain SSH soft reboot
  (`reboot`, then poll until SSH comes back and a command actually
  succeeds) instead of the Alpaca TAC. Logs are pulled via SFTP instead
  of `adb`. No TAC or adb dependency in this mode.

These two transports are mutually exclusive: pass --host for SSH mode, or
--com-port (or nothing, for auto-detection) for serial mode.

Like the retry/poll loops below, `adb root` right after adbd is freshly
enabled can race adbd's own startup and fail (confirmed live: an identical
manual retry succeeded a moment later) -- a fixed retry delay means every
retry across every run lands on that same race window the same way, so
these loops use randomized (jittered) backoff instead.

Run standalone against an already-flashed device:
    py -3 capture.py
    py -3 capture.py --build-path "Build-7"   # override the auto label
    py -3 capture.py --host 192.168.1.50 --user root   # SSH mode

Run with the ARM64 Python launcher on this machine (`py -3`), or with a
regular Windows Python install (`python3`).
Requires: pip install pyserial comtypes  (adb also needed, on PATH)
SSH mode additionally requires: pip install paramiko
"""

from __future__ import annotations

import argparse
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


def _ensure_report_importable() -> None:
    """report.py lives in the sibling `report/` skill folder, two
    directories up from this file (bootbench/capture/scripts/capture.py ->
    bootbench/report/report.py) -- capture.py has no report-rendering logic
    of its own and delegates to report.py for recording + rendering every
    run it captures. regression_analysis.py needs no such shim: it's
    co-located in this same scripts/ folder, and Python already puts a
    script's own directory on sys.path when it's run directly."""
    here = Path(__file__).resolve().parent  # .../capture/scripts
    report_dir = here.parent.parent / "report"  # .../report
    if (report_dir / "report.py").is_file() and str(report_dir) not in sys.path:
        sys.path.insert(0, str(report_dir))


_ensure_report_importable()
import report
import regression_analysis

DELTA_MERGE_S = 0.05  # threshold for noting graphical.target overshoot

COLLECT_SCRIPT_REMOTE_PATH = "/data/collect_boot_logs.sh"
COLLECT_SCRIPT_HEREDOC_MARKER = "BOOT_LOG_SCRIPT_EOF"

# Jitter bounds for every retry/poll loop in this module (serial console
# probing, login nudges, TAC open, adb root/device-wait) -- a fixed
# interval means every retry across every run lands on the same wall-clock
# offset from the triggering event (USB re-enumeration, TAC COM churn,
# adbd startup), repeating the same miss if that offset lands inside a
# transient busy window. Jitter spreads attempts across the window instead.
JITTER_MIN_FACTOR = 0.5
JITTER_MAX_FACTOR = 1.5


def _jittered_delay(base_delay_s: float) -> float:
    return random.uniform(base_delay_s * JITTER_MIN_FACTOR, base_delay_s * JITTER_MAX_FACTOR)


COLLECT_SCRIPT_CONTENT = """#!/bin/sh
# collect_boot_logs.sh
# This script runs locally on the target to collect boot optimization logs.

echo "=========================================="
echo "    Target Boot Log Collection Script     "
echo "=========================================="

setenforce 0

echo "[0/4] Disabling Remote FS"
systemctl disable rmtfs.service
systemctl mask rmtfs.service

#echo "[1/4] Disabling Network Wait-Online Services..."
#systemctl stop systemd-networkd-wait-online.service 2>/dev/null
#systemctl disable systemd-networkd-wait-online.service 2>/dev/null
#systemctl disable NetworkManager-wait-online.service 2>/dev/null
#systemctl mask NetworkManager-wait-online.service 2>/dev/null

echo "[2/4] Preparing /data/Logs directory..."
mkdir -p /data/Logs
rm -f /data/Logs/*

echo "Turning off kernel tracing..."
echo 0 > /sys/kernel/tracing/tracing_on 2>/dev/null || true

echo "[3/4] Collecting System Logs..."
cp /sys/kernel/tracing/trace /data/Logs/Boot_Trace.txt 2>/dev/null || true
systemd-analyze > /data/Logs/systemd_analyze.txt
systemd-analyze blame > /data/Logs/blame_systemd.txt
systemd-analyze plot > /data/Logs/plot_systemd.svg
systemctl > /data/Logs/blame_systemdsystemctl.txt
journalctl --output=short-monotonic -b --no-pager -l > /data/Logs/journalctl.log
dmesg > /data/Logs/dmesg.txt
systemd-analyze critical-chain > /data/Logs/systemd-analyze_critical_chain.txt
systemd-analyze critical-chain sysinit.target > /data/Logs/systemd-analyze_critical_chain_sysinit.txt

# Extract config if present
if [ -f /proc/config.gz ]; then
    zcat /proc/config.gz > /data/Logs/proc_config.txt
fi

cat /proc/cmdline > /data/Logs/kernel_cmdline.txt
lsmod > /data/Logs/lsmod.txt

echo
echo "------------------------------------------"
cat /data/Logs/systemd_analyze.txt
echo "------------------------------------------"

echo "[4/4] Generating Summary (brief.txt)..."
"""


# ---------------------------------------------------------------------------
# Serial console -- login and command execution. No flashing happens over
# this console; it's used only for login, debug-cmdline injection, and
# triggering the on-device log-collection script.
# ---------------------------------------------------------------------------

SERIAL_BAUD = 115200
LOGIN_USER = "root"
LOGIN_PASSWORD = "oelinux123"

SERIAL_HOSTNAME_RE = re.compile(r"hostname=([^;\\]+)")
LOGIN_PROMPT_RE = re.compile(r"\S+\s+login:")
PASSWORD_PROMPT_RE = re.compile(r"[Pp]assword:\s*$", re.MULTILINE)
SHELL_PROMPT_RE = re.compile(r"[#\$]\s*$", re.MULTILINE)
DONE_MARKER_RE = re.compile(r"__DONE_(\d+)__")

# Async kernel log lines (driver warnings, timeouts, etc.) print to the serial
# console at arbitrary times, including interleaved mid-line with a login/
# password/shell prompt with no newline in between -- which breaks the `$`
# (end-of-line) anchor in the prompt regexes above. Strip them before prompt
# matching so trailing kernel noise doesn't hide a real prompt.
KERNEL_LOG_NOISE_RE = re.compile(r"\[\s*\d+\.\d+\]\s*[^\r\n]*")

# This device's shell wraps every command's output in an OSC shell-integration
# marker (ESC ] 3008;start=...;hostname=...;cwd=...ESC \) with no newline
# separating it from the actual output -- invisible when printed to a real
# terminal (an ANSI-aware terminal consumes it silently), but present verbatim
# in the raw bytes read here, so a `uname -n`/command-output capture that
# doesn't strip it gets that marker's own "hostname="/"cwd=" text spliced into
# the result. Always stripped in run_serial_command, regardless of
# strip_noise -- unlike kernel log noise, it's never legitimate command
# payload (e.g. dmesg would never emit this OSC format itself).
OSC_SEQUENCE_RE = re.compile(r"\x1b\]\d+;.*?\x1b\\", re.DOTALL)

# Fallback default for open_console() when no explicit boot_timeout is given.
DEFAULT_LOGIN_TIMEOUT_S = 60


def _strip_kernel_log_noise(text: str) -> str:
    return KERNEL_LOG_NOISE_RE.sub("", text)


def _strip_osc_sequences(text: str) -> str:
    return OSC_SEQUENCE_RE.sub("", text)


def _drain(ser, duration_s: float = 0.5) -> str:
    time.sleep(duration_s)
    data = ser.read(65536)
    return data.decode("utf-8", errors="replace")


def _probe_serial_port(port_name: str) -> str | None:
    """Returns the raw console text read from port_name, or None if the port
    couldn't be opened. Used to auto-detect which COM port is the target's own
    console -- other consoles on the board (e.g. a co-processor debug shell)
    also enumerate and may show a bare shell prompt, so a login prompt (which
    carries the target's own hostname) is required to be confident, and a bare
    shell prompt is only accepted as a fallback if no port shows a login prompt."""
    import serial

    try:
        with serial.Serial(port_name, SERIAL_BAUD, timeout=1) as ser:
            ser.reset_input_buffer()
            ser.write(b"\r\n")
            return _drain(ser, 1.0)
    except OSError:
        # Port busy (e.g. held open by another app or the TAC control channel) or
        # doesn't exist -- not a candidate.
        return None


def find_console_port(timeout_s: int = DEFAULT_LOGIN_TIMEOUT_S) -> str:
    """Repeatedly sweeps every enumerated COM port until one shows a live
    target console, preferring the strongest signal seen so far. A single
    sweep run right after a power-cycle can catch the real target console
    before it has booted far enough to print anything, while an unrelated
    already-logged-in port (e.g. a co-processor debug shell left open on this
    machine) answers immediately -- so a one-shot scan would lock onto that
    wrong port forever. Looping gives the real console the full timeout to
    appear and always prefers a hostname/login match over a bare shell-prompt
    fallback, regardless of which one is seen first.

    The probe order is reshuffled on every sweep (rather than always walking
    list_ports.comports() in the same OS-enumeration order) so a slow-to-open
    port is never pinned to the same position -- each sweep costs ~1s/port
    from _probe_serial_port's own read window, so a fixed order means the
    last port in that order always pays the full accumulated delay before
    being checked at all."""
    from serial.tools import list_ports

    deadline = time.time() + timeout_s
    shell_prompt_fallback = None
    while True:
        candidates = [p.device for p in list_ports.comports()]
        if not candidates:
            raise RuntimeError("No serial (COM) ports found on this machine.")
        random.shuffle(candidates)

        login_fallback = None
        for port_name in candidates:
            text = _probe_serial_port(port_name)
            if text is None:
                continue
            # OSC hostname marker is the strongest signal -- it names the target's
            # own hostname directly, unlike a bare login/shell prompt which other
            # consoles on the board (e.g. a co-processor debug shell) can also show.
            if SERIAL_HOSTNAME_RE.search(text):
                return port_name
            clean = _strip_kernel_log_noise(text)
            if login_fallback is None and LOGIN_PROMPT_RE.search(clean):
                login_fallback = port_name
            if shell_prompt_fallback is None and SHELL_PROMPT_RE.search(clean.strip()):
                shell_prompt_fallback = port_name

        if login_fallback:
            return login_fallback

        if time.time() >= deadline:
            if shell_prompt_fallback:
                return shell_prompt_fallback
            raise RuntimeError(
                f"No live console found on any of: {', '.join(candidates)} within {timeout_s}s. "
                "Pass --com-port explicitly, or check the device is powered on."
            )


def wait_for_login_prompt(ser, timeout_s: int) -> str:
    """Poll the console until a login: prompt appears, nudging with a
    newline at a jittered interval (first boot after a power-cycle can take
    minutes; a fixed nudge interval risks always landing mid-boot-message-
    burst on a device that logs verbosely at a steady cadence, drowning the
    nudge)."""
    deadline = time.time() + timeout_s
    buf = ""
    next_nudge_at = time.time() + _jittered_delay(5.0)
    while time.time() < deadline:
        buf += _drain(ser, 1.0)
        clean = _strip_kernel_log_noise(buf)
        if LOGIN_PROMPT_RE.search(clean) or SHELL_PROMPT_RE.search(clean.strip()):
            return buf
        if time.time() >= next_nudge_at:
            ser.write(b"\r\n")
            next_nudge_at = time.time() + _jittered_delay(5.0)
        buf = buf[-4096:]
    raise RuntimeError(
        f"No login/shell prompt seen on serial console within {timeout_s}s. "
        "Device may still be booting or stuck -- check the console manually."
    )


def login(ser, timeout_s: int):
    print(f"Waiting up to {timeout_s}s for a login/shell prompt on serial console...")
    buf = wait_for_login_prompt(ser, timeout_s)
    clean_buf = _strip_kernel_log_noise(buf)

    if SHELL_PROMPT_RE.search(clean_buf.strip()) and not LOGIN_PROMPT_RE.search(clean_buf):
        print("Already at a shell prompt (no login required).")
        return

    print(f"Login prompt seen, logging in as {LOGIN_USER}...")
    ser.write(f"{LOGIN_USER}\r\n".encode())
    time.sleep(1.0)
    resp = _drain(ser, 2.0)

    if PASSWORD_PROMPT_RE.search(_strip_kernel_log_noise(resp)):
        ser.write(f"{LOGIN_PASSWORD}\r\n".encode())
        time.sleep(1.0)
        resp = _drain(ser, 2.0)

    deadline = time.time() + 15
    while not SHELL_PROMPT_RE.search(_strip_kernel_log_noise(resp).strip()) and time.time() < deadline:
        resp += _drain(ser, 1.0)

    if not SHELL_PROMPT_RE.search(_strip_kernel_log_noise(resp).strip()):
        raise RuntimeError(f"Login did not reach a shell prompt. Last console output:\n{resp}")
    print("Logged in.")


def run_serial_command(ser, cmd: str, timeout_s: int = 30, strip_noise: bool = True) -> str:
    """Runs cmd over the serial console, using an echoed exit-code marker to
    detect completion (there's no pexpect-style framework on a raw line).
    strip_noise=False must be used for commands whose own output legitimately
    uses the same `[ddd.ddd] ...` bracket format that _strip_kernel_log_noise
    strips (e.g. `dmesg`) -- stripping would delete the actual payload, not
    just async interleaved noise."""
    marker_cmd = f"{cmd}; echo __DONE_$?__"
    # The console echoes back exactly what was written, but the terminal's own
    # line-wrapping can insert a \r\n at an arbitrary point *inside* that echo
    # -- confirmed live: `echo __DONE_$?__` echoed back as "echo _" + newline +
    # "_DONE_$?__". A single-line substring match (the old approach) silently
    # fails to recognize a wrapped echo, leaving it stuck to the front of the
    # real command output. Tolerate an optional wrap between every character.
    echo_re = re.compile(r"\r?\n?".join(re.escape(c) for c in marker_cmd))
    ser.reset_input_buffer()
    ser.write((marker_cmd + "\r\n").encode())

    deadline = time.time() + timeout_s
    buf = ""
    while time.time() < deadline:
        buf += _drain(ser, 0.5)
        m = DONE_MARKER_RE.search(buf)
        if m:
            rc = int(m.group(1))
            output = _strip_osc_sequences(buf[:m.start()])
            # Drop the echoed command text, wherever it appears in the output
            # (kernel-log noise may have interleaved before it even reached
            # the console) -- echo_re tolerates the mid-echo line wrap.
            em = echo_re.search(output)
            if em:
                output = output[em.end():]
            lines = output.splitlines()
            text = "\n".join(lines).strip()
            if strip_noise:
                text = _strip_kernel_log_noise(text).strip()
            if rc != 0:
                raise RuntimeError(f"Command failed (rc={rc}): {cmd!r}\nOutput:\n{text}")
            return text
    raise RuntimeError(f"Timed out waiting for command to complete: {cmd!r}\nPartial output:\n{buf}")


def open_console(com_port: str = None, boot_timeout: int = DEFAULT_LOGIN_TIMEOUT_S):
    """Opens the serial console, waits for a login/shell prompt, and logs in.
    Returns (ser, com_port_used); caller is responsible for closing ser."""
    import serial

    if com_port is None:
        com_port = find_console_port(timeout_s=boot_timeout)

    ser = serial.Serial(com_port, SERIAL_BAUD, timeout=1)
    login(ser, boot_timeout)
    return ser, com_port


class SerialConn:
    """Uniform run()/try_run()/close() wrapper around an open pyserial
    `ser` object, so the mid-level orchestration functions below (device
    info, debug cmdline injection, log collection) can be written once
    against either this or SshConn. Every call simply forwards to the
    existing run_serial_command() -- zero behavior change for serial
    mode."""

    def __init__(self, ser):
        self.ser = ser

    def run(self, cmd: str, timeout_s: int = 30, strip_noise: bool = True) -> str:
        return run_serial_command(self.ser, cmd, timeout_s=timeout_s, strip_noise=strip_noise)

    def try_run(self, cmd: str) -> str | None:
        try:
            return self.run(cmd)
        except RuntimeError:
            return None

    def close(self):
        self.ser.close()


# ---------------------------------------------------------------------------
# SSH -- alternative to the serial console for login and command execution.
# Selected by passing --host; mutually exclusive with --com-port/--tac-port.
# paramiko is imported lazily (inside SshConn.__init__) so serial-only users
# never need it installed.
# ---------------------------------------------------------------------------

SSH_CONNECT_TIMEOUT_S = 15


class SshConn:
    """Uniform run()/try_run()/close() wrapper around a paramiko SSH
    connection, mirroring SerialConn's interface so the same mid-level
    orchestration functions work against either transport."""

    def __init__(self, host: str, user: str = "root", password: str = None,
                 key_file: str = None, port: int = 22, _client=None):
        self.host = host
        self.user = user
        self.password = password
        self.key_file = key_file
        self.port = port
        self._client = _client or self._connect()

    def _connect(self):
        import paramiko

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs = {
            "hostname": self.host, "port": self.port, "username": self.user,
            "timeout": SSH_CONNECT_TIMEOUT_S,
        }
        if self.key_file:
            connect_kwargs["key_filename"] = self.key_file
        if self.password:
            connect_kwargs["password"] = self.password
        client.connect(**connect_kwargs)
        return client

    def run(self, cmd: str, timeout_s: int = 30, strip_noise: bool = True) -> str:
        # strip_noise is accepted (not used) for interface parity with
        # SerialConn.run -- there's no kernel-log-interleaving problem over
        # a clean SSH channel, unlike the raw serial console.
        _stdin, stdout, stderr = self._client.exec_command(cmd, timeout=timeout_s)
        rc = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace").strip()
        if rc != 0:
            err = stderr.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Command failed (rc={rc}): {cmd!r}\nOutput:\n{out}\n{err}")
        return out

    def try_run(self, cmd: str) -> str | None:
        try:
            return self.run(cmd)
        except RuntimeError:
            return None

    def pull_dir(self, remote_dir: str, local_dir: Path) -> bool:
        """Recursively pulls remote_dir into local_dir via SFTP, mirroring
        the remote directory structure (there is no single 'sftp pull -r'
        call in paramiko)."""
        import stat as stat_module

        local_dir.mkdir(parents=True, exist_ok=True)
        sftp = self._client.open_sftp()
        try:
            found_any = False
            for entry in sftp.listdir_attr(remote_dir):
                remote_path = f"{remote_dir}/{entry.filename}"
                local_path = local_dir / entry.filename
                if stat_module.S_ISDIR(entry.st_mode):
                    self.pull_dir(remote_path, local_path)
                else:
                    sftp.get(remote_path, str(local_path))
                found_any = True
            return found_any
        finally:
            sftp.close()

    def close(self):
        self._client.close()

    def reboot_and_wait(self, timeout_s: int) -> "SshConn":
        """Issues a soft `reboot` over SSH, waits for the connection to
        drop then come back, and returns a *new*, connected SshConn (the
        old one's transport is gone once the device actually reboots).
        Used in place of the Alpaca TAC power-cycle when operating over
        SSH -- no hardware power control available/needed in this mode."""
        print(f"Rebooting {self.host} over SSH ...")
        try:
            self.try_run("reboot")
        finally:
            self.close()

        deadline = time.time() + timeout_s
        # Give the device a moment to actually go down before polling for
        # it to come back -- otherwise the first few reconnect attempts can
        # race the reboot and succeed against the still-dying old session.
        time.sleep(5)
        last_error = None
        while time.time() < deadline:
            try:
                conn = SshConn(self.host, self.user, self.password, self.key_file, self.port)
                conn.run("true")  # verify a command actually succeeds, not just that the port is open
                return conn
            except Exception as e:
                last_error = e
                time.sleep(_jittered_delay(5.0))
        raise RuntimeError(
            f"{self.host} did not come back over SSH within {timeout_s}s after reboot: {last_error!r}"
        )


def detect_target(conn) -> str:
    target = conn.run("uname -n")
    if not target:
        raise RuntimeError("Could not determine target name from 'uname -n' (empty output)")
    return target


# ---------------------------------------------------------------------------
# Device info (for the report's "Device Information" panel)
# ---------------------------------------------------------------------------

OS_RELEASE_LINE_RE = re.compile(r'^([A-Z_]+)=(.*)$')


def _parse_os_release(text: str) -> dict:
    result = {}
    for line in text.splitlines():
        m = OS_RELEASE_LINE_RE.match(line.strip())
        if m:
            result[m.group(1)] = m.group(2).strip().strip('"')
    return result


def collect_device_info(conn) -> dict:
    """Collects device identification fields over the active connection
    (serial console or SSH) for the report's Device Information panel:
    Machine (device-tree model), SoC (soc0 machine + revision), OS
    (/etc/os-release), Kernel (uname -r), and Captured (when this snapshot
    was taken). Best-effort per field -- a missing one is simply omitted
    from the rendered panel rather than aborting the capture.

    Serial is deliberately NOT collected here. soc0/serial_number is the
    SoC's own serial, which is not the id this device is actually addressed
    by over adb -- capturing that instead reports a number that doesn't let
    anyone find the device via `adb devices`/`adb -s <serial>`. The report's
    Serial field is populated later (from `adb devices`'s own device id in
    serial mode, see wait_for_adb_device, or from user@host in SSH mode)."""
    model = conn.try_run("cat /proc/device-tree/model")
    machine = conn.try_run("cat /sys/devices/soc0/machine")
    revision = conn.try_run("cat /sys/devices/soc0/revision")
    kernel = conn.try_run("uname -r")
    os_release_text = conn.try_run("cat /etc/os-release")

    soc = f"{machine} rev {revision}" if machine and revision else machine

    os_name = None
    if os_release_text:
        os_fields = _parse_os_release(os_release_text)
        os_name = os_fields.get("NAME") or os_fields.get("PRETTY_NAME")

    return {
        "machine": model,
        "soc": soc,
        "os": os_name,
        "kernel": kernel,
        "captured": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# Alpaca TAC -- power-cycle control ONLY (no EDL/flash trigger here). Used
# solely to force a clean, repeatable reboot between boots in the capture
# loop; capture never puts the device into EDL/Sahara mode.
# ---------------------------------------------------------------------------

TAC_PORT_NAME = None  # auto-detected from the single connected TAC device if None
_tac_server_ref = None  # keeps the AlpacaTACServer COM object alive; see _open_tac()


def _open_tac(retries: int = 3, retry_delay_s: float = 2.0):
    import comtypes.client as cc

    # Keep a reference to the top-level AlpacaTACServer COM object at module scope.
    # Create_TAC_Server() returns a *child* COM object (ITACServer); if nothing
    # keeps the parent alive, Python garbage-collects it as soon as this function
    # returns, which disconnects the child's interface and makes every subsequent
    # call (e.g. PowerOnButton, Close) fail with "The object invoked has
    # disconnected from its clients."
    global _tac_server_ref

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            _tac_server_ref = cc.CreateObject("TACCOM.AlpacaTACServer")
            tac = _tac_server_ref.Create_TAC_Server()
            count = tac.Get_Device_Count()
            if count == 0:
                raise RuntimeError("No Alpaca TAC devices found. Is the debug board connected?")

            port = TAC_PORT_NAME
            if port is None:
                if count > 1:
                    raise RuntimeError(
                        f"Multiple TAC devices found ({count}); pass --tac-port to disambiguate."
                    )
                port = tac.Get_PortName(0)

            if not tac.OpenByName(port):
                raise RuntimeError(f"Failed to open TAC device on port {port}")

            print(f"Opened TAC device: {tac.Get_Name()} ({tac.Get_HardwareVersion()}) on port {port}")
            return tac
        except Exception as e:
            last_error = e
            _tac_server_ref = None
            if attempt < retries:
                delay = _jittered_delay(retry_delay_s)
                print(f"TAC open attempt {attempt}/{retries} failed ({e!r}); retrying in {delay:.1f}s...")
                time.sleep(delay)

    raise RuntimeError(f"Could not open Alpaca TAC server after {retries} attempts: {last_error!r}")


def power_cycle_device():
    """Power-cycles the device via the Alpaca TAC (off, pause, on) for a
    clean reboot between boots. The Alpaca TAC has no single 'reset' command
    -- only discrete PowerOffButton / PowerOnButton -- so this is off, pause,
    on."""
    tac = _open_tac()
    try:
        print("Powering device off...")
        tac.PowerOffButton()
        time.sleep(2)
        print("Powering device on...")
        tac.PowerOnButton()
    finally:
        tac.Close()


# ---------------------------------------------------------------------------
# Build labeling -- no nightly-build-share discovery. Each capture that
# doesn't get an explicit --build-path is labeled "Build-<N>", auto-
# incrementing per target from whatever's already recorded for it.
# ---------------------------------------------------------------------------

BUILD_LABEL_RE = re.compile(r"^Build-(\d+)$")


def next_build_label(target: str) -> str:
    """Returns the next auto-incrementing build label ("Build-1", "Build-2",
    ...) for this target: one more than the highest "Build-<N>" label
    already recorded in that target's bootchart-data-<slug>.json, or
    "Build-1" if no prior run used this naming scheme (or no runs exist yet
    for this target). A user-supplied --build-path value that doesn't match
    "Build-<N>" (e.g. a custom label) is simply left alone and never
    collides with this counter, since it's only ever read back here."""
    data_path, _ = report._data_html_paths(target)
    data = report.load_data(data_path)
    highest = 0
    for run in data.get("runs", []):
        m = BUILD_LABEL_RE.match(run.get("build_path") or "")
        if m:
            highest = max(highest, int(m.group(1)))
    return f"Build-{highest + 1}"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

TIME_TOKEN_RE = re.compile(r"([\d.]+)(ms|s)")


def _to_seconds(num: str, unit: str) -> float:
    value = float(num)
    return value / 1000.0 if unit == "ms" else value


def parse_systemd_time(text: str) -> dict:
    """Parses the 'Startup finished in ...' line from `systemd-analyze time`."""
    m = re.search(
        r"Startup finished in (.+?)\s*=\s*([\d.]+)(ms|s)",
        text,
    )
    if not m:
        raise RuntimeError(f"Could not parse `systemd-analyze time` output:\n{text}")
    parts_text, total_num, total_unit = m.group(1), m.group(2), m.group(3)

    parts = {}
    for num, unit, label in re.findall(r"([\d.]+)(ms|s)\s*\((\w+)\)", parts_text):
        parts[label] = _to_seconds(num, unit)

    result = {
        "firmware": parts.get("firmware", 0.0),
        "loader": parts.get("loader", 0.0),
        "kernel": parts.get("kernel", 0.0),
        "userspace": parts.get("userspace", 0.0),
        "total": _to_seconds(total_num, total_unit),
    }

    gm = re.search(r"graphical\.target reached after ([\d.]+)(ms|s)", text)
    if gm:
        result["graphical_userspace"] = _to_seconds(gm.group(1), gm.group(2))
    return result


def parse_critical_chain(text: str) -> tuple:
    """Returns (top_level_userspace_seconds, chain_names_top_to_bottom).
    The '@Xs' on each line is time-since-userspace-start (PID 1), NOT
    time-since-power-on -- confirmed against real device output where the
    top target's @ value matches systemd-analyze time's userspace figure."""
    lines = [l for l in text.splitlines() if re.search(r"@[\d.]+(?:ms|s)", l)]
    if not lines:
        raise RuntimeError(f"Could not parse `systemd-analyze critical-chain` output:\n{text}")

    top_m = re.search(r"@([\d.]+)(ms|s)", lines[0])
    if not top_m:
        raise RuntimeError(f"Could not find '@time' on first critical-chain line: {lines[0]!r}")
    top_seconds = _to_seconds(top_m.group(1), top_m.group(2))

    chain = []
    for line in lines:
        m = re.search(r"[`\-\s]*([\w.@:\\+-]+\.(?:service|target|socket|device|mount|slice))\s*@", line)
        if m:
            chain.append(m.group(1))
    return top_seconds, chain


STAGE_HITTER_RE = re.compile(
    r"[`\-\s]*([\w.@:\\+-]+\.(?:service|target|socket|device|mount|slice))"
    r"\s*@[\d.]+(?:ms|s)\s+\+([\d.]+)(ms|s)"
)


def parse_stage_hitters(text: str, top_n: int = 6) -> list:
    """Ranks units in a `systemd-analyze critical-chain` block by their own
    '+cost' (time the unit itself took to start), descending. Unlike
    parse_critical_chain's `chain` (top-to-bottom dependency order), this is
    a ranking -- used for per-stage high hitters (e.g. sysinit_svc), where
    there's no `blame`-style command scoped to a single target."""
    costs = []
    for m in STAGE_HITTER_RE.finditer(text):
        name, num, unit = m.group(1), m.group(2), m.group(3)
        costs.append((_to_seconds(num, unit), name))
    costs.sort(key=lambda t: t[0], reverse=True)
    return [{"name": name, "time": f"{secs:.3f} s"} for secs, name in costs[:top_n]]


def parse_blame(text: str, top_n: int = 6) -> list:
    hitters = []
    for line in text.splitlines():
        m = re.match(r"\s*([\d.]+)(ms|s)\s+(\S+)", line)
        if not m:
            continue
        seconds = _to_seconds(m.group(1), m.group(2))
        hitters.append({"name": m.group(3), "time": f"{seconds:.3f} s"})
        if len(hitters) >= top_n:
            break
    if not hitters:
        raise RuntimeError(f"Could not parse `systemd-analyze blame` output:\n{text}")
    return hitters


def parse_dmesg_milestones(text: str) -> dict:
    def find(pattern):
        m = re.search(pattern, text)
        if not m:
            raise RuntimeError(f"dmesg milestone not found ({pattern!r}) in:\n{text}")
        return float(m.group(1))

    return {
        "init_exec": find(r"\[\s*([\d.]+)\]\s*Run /init as init process"),
        "epoch_advanced": find(r"\[\s*([\d.]+)\]\s*systemd\[1\]: System time advanced to (?:built-in epoch|timestamp on \S+:)"),
        "systemd_running": find(r"\[\s*([\d.]+)\]\s*systemd\[1\]: systemd .* running in system mode"),
    }


INITCALL_PROBE_RE = re.compile(r"(?:initcall|probe of) (\S+) returned (-?\d+) after (\d+) usecs")
PROBE_FAIL_RE = re.compile(r"(\S+): probe with driver \S+ failed with error (-?\d+)")


def parse_kernel_hitters(dmesg_text: str, top_n_slow: int = 3, max_failures: int = 3):
    """Extracts debug-cmdline-derived kernel init signal from a debug-phase
    dmesg (requires initcall_debug to be active -- see ensure_debug_cmdline_params):
    the top_n_slow slowest probes/initcalls by usecs, followed by up to
    max_failures probe/initcall failures (deduped by name). Returns None if
    dmesg carries no initcall/probe timing at all -- a content-based signal
    that debug cmdline params weren't active for this boot, correct
    regardless of the reason (Type #2 bootloader, --resume-pull, etc.)."""
    probe_matches = list(INITCALL_PROBE_RE.finditer(dmesg_text))
    fail_matches = list(PROBE_FAIL_RE.finditer(dmesg_text))
    if not probe_matches and not fail_matches:
        return None

    slow = sorted(
        ((m.group(1), int(m.group(2)), int(m.group(3))) for m in probe_matches),
        key=lambda t: t[2], reverse=True,
    )[:top_n_slow]
    slow_list = [
        {"name": name, "time": f"{usecs / 1_000_000:.3f} s", "note": "slowest probe/initcall"}
        for name, _rc, usecs in slow
    ]

    failures = []
    seen = set()
    for m in probe_matches:
        name, rc, _usecs = m.group(1), m.group(2), m.group(3)
        if rc == "0" or name in seen:
            continue
        seen.add(name)
        failures.append({"name": name, "time": f"rc={rc}", "note": "probe/initcall failure"})
    for m in fail_matches:
        name, rc = m.group(1), m.group(2)
        if name in seen:
            continue
        seen.add(name)
        failures.append({"name": name, "time": f"error {rc}", "note": "probe failure"})
    failures = failures[:max_failures]

    return slow_list + failures


FAILED_UNIT_RE = re.compile(
    r"^[^\w]*([\w.@:\\+-]+\.(?:service|socket|device|mount|target|slice|path|timer))"
    r"\s+(?:loaded|not-found|masked|bad)\s+failed\s+(\S+)",
    re.MULTILINE,
)


def parse_failed_units(systemctl_text: str) -> list:
    """Extracts units `systemctl` reports as ACTIVE=failed from its default
    (no-pager) dump -- the UNIT/LOAD/ACTIVE/SUB/DESCRIPTION table, tolerating
    the leading bullet systemd prints on a failed/degraded line. Returns
    [{"name":, "sub_state":}, ...]; [] if nothing is in a failed state (the
    common case) or the file is empty."""
    return [
        {"name": m.group(1), "sub_state": m.group(2)}
        for m in FAILED_UNIT_RE.finditer(systemctl_text)
    ]


def parse_journalctl_milestone(text: str) -> float:
    """Returns the monotonic timestamp of 'Reached target System
    Initialization.' from journalctl --output=short-monotonic -- this
    milestone isn't reliably present in dmesg's own ring buffer, unlike the
    epoch-advance line, so it's read from the journal instead."""
    m = re.search(r"\[\s*([\d.]+)\]\s*.*Reached target System Initialization\.", text)
    if not m:
        raise RuntimeError(
            f"journalctl milestone not found ('Reached target System Initialization.') in:\n{text}"
        )
    return float(m.group(1))


def build_run(target: str, build_path: str, sat_text: str, cc_text: str,
              cc_sysinit_text: str, blame_text: str, dmesg_text: str, journalctl_text: str,
              systemctl_text: str = "") -> dict:
    sat = parse_systemd_time(sat_text)
    cc_multiuser_s, chain = parse_critical_chain(cc_text)
    hitters = parse_blame(blame_text)
    sysinit_hitters = parse_stage_hitters(cc_sysinit_text)
    kernel_hitters = parse_kernel_hitters(dmesg_text)
    failed_units = parse_failed_units(systemctl_text)
    milestones = parse_dmesg_milestones(dmesg_text)
    sysinit_target_s = parse_journalctl_milestone(journalctl_text)

    nhlos = sat["firmware"] + sat["loader"]
    kernel = sat["kernel"]

    init_exec = milestones["init_exec"]
    epoch_advanced = milestones["epoch_advanced"]
    initramfs_s = epoch_advanced - init_exec
    sysinit_svc = sysinit_target_s - epoch_advanced

    total_sysinit = nhlos + kernel + initramfs_s + sysinit_svc
    total_multiuser = nhlos + kernel + cc_multiuser_s

    multiuser_note = ""
    grand_total = nhlos + kernel + sat["userspace"]
    if grand_total - total_multiuser >= DELTA_MERGE_S:
        multiuser_note = f"≈{grand_total:.3f} s incl. graphical.target"

    kernel_metric = {"value": f"{kernel:.3f} s", "note": ""}
    if kernel_hitters:
        kernel_metric["hitters"] = kernel_hitters

    run = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "build_path": build_path,
        "metrics": {
            "nhlos": {
                "value": f"{nhlos:.3f} s",
                "note": f"{sat['firmware']:.3f} s firmware + {sat['loader']:.3f} s loader",
            },
            "kernel": kernel_metric,
            "initramfs": {
                "value": f"{initramfs_s:.3f} s",
                "note": f"{init_exec:.3f} s → {epoch_advanced:.3f} s",
            },
            "sysinit_svc": {
                "value": f"{sysinit_svc:.3f} s",
                "note": f"{epoch_advanced:.3f} s → {sysinit_target_s:.3f} s",
                "hitters": sysinit_hitters,
            },
            "total_sysinit": {"value": f"{total_sysinit:.3f} s", "note": ""},
            "total_multiuser": {
                "value": f"{total_multiuser:.3f} s",
                "note": multiuser_note,
                "hitters": hitters,
            },
            "failed_units": failed_units,
        },
        "critical_chain": chain,
        "source": (
            f"systemd-analyze time -> Startup finished in {sat['firmware']:.3f}s (firmware) + "
            f"{sat['loader']:.3f}s (loader) + {sat['kernel']:.3f}s (kernel) + "
            f"{sat['userspace']:.3f}s (userspace) = {sat['total']:.3f}s; "
            f"dmesg: Run /init @{init_exec:.3f}s, System time advanced to built-in epoch @{epoch_advanced:.3f}s, "
            f"systemd running @{milestones['systemd_running']:.3f}s; "
            f"journalctl: Reached target System Initialization @{sysinit_target_s:.3f}s; "
            f"initramfs = Run/init -> epoch-advance; sysinit_svc = epoch-advance -> sysinit.target; "
            f"sysinit hitters from `systemd-analyze critical-chain sysinit.target` "
            f"(+cost per unit); multi-user hitters from `systemd-analyze blame`; "
            f"kernel hitters (debug boots only) from initcall_debug dmesg output; "
            f"failed_units from `systemctl` (ACTIVE=failed units)"
        ),
    }
    return run


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

DEBUG_CMDLINE_HEREDOC_MARKER = "DEBUG_CMDLINE_EOF"

BOOTCTL_SOURCE_RE = re.compile(r"source:\s*(\S+)")

# name-only presence check: any value counts as satisfied (the user may
# want a different buffer size than our default).
LOG_BUF_LEN_RE = re.compile(r"(?<![^\s])log_buf_len=\S+")
LOG_BUF_LEN_PARAM = "log_buf_len=4M"

# exact-match checks: token must be present verbatim (initcall_debug) or
# with exactly this value (systemd.log_level=debug) -- unlike log_buf_len
# above, a differing value does not count as already satisfied.
EXACT_CMDLINE_PARAMS = [
    (re.compile(r"(?<![^\s])initcall_debug(?![^\s])"), "initcall_debug"),
    (re.compile(r"(?<![^\s])systemd\.log_level=debug(?![^\s])"), "systemd.log_level=debug"),
]
# Matches systemd.log_level= with ANY value, used only to strip a stale
# differing value before re-appending the correct one -- otherwise the
# line would end up with two conflicting systemd.log_level= tokens.
STALE_LOG_LEVEL_RE = re.compile(r"(?<![^\s])systemd\.log_level=\S+")


def resolve_boot_entry(conn):
    """Determines the on-device bootloader entry file to edit for debug
    cmdline params, without hardcoding a path. Runs `bootctl status | grep
    type` -- if the device isn't a Boot Loader Specification Type #1 (i.e.
    Type #2 or unrecognized), there's no per-entry options line to edit, so
    returns None. Otherwise runs `bootctl status | grep conf` and pulls the
    current entry's real `source:` path out of that output. Returns the
    resolved path string, or None if not Type #1."""
    type_output = conn.run("bootctl status | grep type")
    if "Type #1" not in type_output:
        return None

    conf_output = conn.run("bootctl status | grep conf")
    match = BOOTCTL_SOURCE_RE.search(conf_output)
    if not match:
        raise RuntimeError(
            "bootctl status reports Boot Loader Specification Type #1, but no "
            f"'source:' line was found in its conf output to resolve the current "
            f"entry file: {conf_output!r}"
        )
    return match.group(1)


def ensure_debug_cmdline_params(conn):
    """Adds any missing debug kernel cmdline params (initcall_debug,
    log_buf_len=4M, systemd.log_level=debug) to the 'options' line of the
    device's current bootloader entry (resolved dynamically via
    resolve_boot_entry, never hardcoded), then syncs. Idempotent and
    order-independent: only params that are missing (or, for
    systemd.log_level, present with the wrong value) are added/corrected;
    params already present are left untouched. Never reverted once
    added -- intentional, so a later capture run doesn't need to redo
    this if the device wasn't reflashed in between. Returns the entry path
    that was edited, or None if the device is Boot Loader Specification
    Type #2 (or unrecognized) and has no per-entry options line to edit."""
    entry_path = resolve_boot_entry(conn)
    if entry_path is None:
        print(
            "bootctl reports Boot Loader Specification Type #2 (or unrecognized) "
            "-- no per-entry options line to edit; skipping debug kernel cmdline params."
        )
        return None

    try:
        conn.run(f"test -f {entry_path}")
    except RuntimeError as e:
        # Checked via exit code (rc!=0), not by parsing echoed EXISTS/MISSING
        # text -- async kernel log lines (e.g. driver probe noise) can print
        # to the console with no newline before the echo, and
        # _strip_kernel_log_noise strips to the end of that physical line,
        # which can eat the echoed text itself and misreport a real file as
        # missing. rc-based detection (same mechanism run_serial_command
        # already uses for every other command) doesn't depend on parsing
        # command output at all.
        raise RuntimeError(
            f"Bootloader entry file not found on device: {entry_path}; "
            f"cannot add debug kernel cmdline params. ({e})"
        ) from e

    content = conn.run(f"cat {entry_path}", strip_noise=False)
    lines = content.splitlines()
    options_idx = next((i for i, l in enumerate(lines) if l.startswith("options ")), None)
    if options_idx is None:
        raise RuntimeError(
            f"No 'options' line found in {entry_path}; "
            "cannot append debug kernel cmdline params."
        )

    options_line = lines[options_idx]
    to_append, already_present, corrected = [], [], []

    if LOG_BUF_LEN_RE.search(options_line):
        already_present.append(LOG_BUF_LEN_PARAM)
    else:
        to_append.append(LOG_BUF_LEN_PARAM)

    for match_re, param in EXACT_CMDLINE_PARAMS:
        if match_re.search(options_line):
            already_present.append(param)
            continue
        stale = STALE_LOG_LEVEL_RE.search(options_line) if param == "systemd.log_level=debug" else None
        if stale:
            options_line = STALE_LOG_LEVEL_RE.sub(param, options_line)
            corrected.append(f"{stale.group(0)} -> {param}")
        else:
            to_append.append(param)

    if not to_append and not corrected:
        print(f"Debug cmdline params already present in {entry_path}, skipping.")
        return entry_path

    if to_append:
        options_line = options_line.rstrip() + " " + " ".join(to_append)
    lines[options_idx] = options_line

    if to_append:
        print(f"Adding debug cmdline params: {', '.join(to_append)}")
    if corrected:
        print(f"Correcting: {', '.join(corrected)}")
    if already_present:
        print(f"Already present, left unchanged: {', '.join(already_present)}")

    new_content = "\n".join(lines) + "\n"
    write_cmd = (
        f"cat > {entry_path} << '{DEBUG_CMDLINE_HEREDOC_MARKER}'\n"
        f"{new_content}"
        f"{DEBUG_CMDLINE_HEREDOC_MARKER}\n"
        f"sync"
    )
    conn.run(write_cmd, timeout_s=60)
    print(f"Updated {entry_path} and synced.")
    return entry_path


def ensure_collect_script(conn):
    """Writes collect_boot_logs.sh to COLLECT_SCRIPT_REMOTE_PATH if it isn't
    already there, and chmod +x's it. Safe to call every boot -- self-healing
    if /data was ever wiped, but only actually writes once per device."""
    check = conn.run(
        f"test -f {COLLECT_SCRIPT_REMOTE_PATH} && echo EXISTS || echo MISSING"
    )
    if check.strip() == "EXISTS":
        return
    print(f"Provisioning {COLLECT_SCRIPT_REMOTE_PATH} on device ...")
    write_cmd = (
        f"cat > {COLLECT_SCRIPT_REMOTE_PATH} << '{COLLECT_SCRIPT_HEREDOC_MARKER}'\n"
        f"{COLLECT_SCRIPT_CONTENT}\n"
        f"{COLLECT_SCRIPT_HEREDOC_MARKER}\n"
        f"chmod +x {COLLECT_SCRIPT_REMOTE_PATH}"
    )
    conn.run(write_cmd, timeout_s=60)


def collect_boot_on_device(conn, log_name: str):
    """Runs collect_boot_logs.sh (provisioning it first if needed), syncs,
    renames /data/Logs -> /data/Logs-<log_name> (e.g. "default-1",
    "debug-2") so it survives the next boot's run, and syncs again."""
    ensure_collect_script(conn)

    print(f"Running collect_boot_logs.sh (boot {log_name}) ...")
    conn.run(f"sh {COLLECT_SCRIPT_REMOTE_PATH}", timeout_s=180)
    conn.run("sync")

    print(f"Renaming /data/Logs -> /data/Logs-{log_name} ...")
    conn.run(f"rm -rf /data/Logs-{log_name}; mv /data/Logs /data/Logs-{log_name}")
    conn.run("sync")


def enable_adb(conn):
    print("Enabling adb ...")
    conn.run("touch /etc/usb-debugging-enabled")
    conn.run("systemctl start android-tools-adbd")


def wait_for_adb_device(timeout_s: int = 60, poll_interval_s: float = 2.0) -> str:
    """Polls `adb devices` until a device shows up in the "device" (ready)
    state, as opposed to absent, "offline", or "unauthorized". Each poll's
    wait is jittered rather than a fixed poll_interval_s, for the same
    reason _adb_root's retry delay is jittered below -- a fixed interval
    means every poll across every run checks at the same offset from
    adbd's own startup, repeating the same miss if that offset happens to
    land inside its not-ready-yet window. Returns that device's id exactly
    as `adb devices` prints it in its first column -- this is the id used
    for the report's Serial field (see build_device_info_for_report), NOT
    anything read from a sysfs node on the device itself."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        result = subprocess.run(["adb", "devices"], capture_output=True, text=True)
        for line in result.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) == 2 and parts[1] == "device":
                return parts[0]
        time.sleep(_jittered_delay(poll_interval_s))
    raise RuntimeError(f"No adb device in 'device' state after {timeout_s}s.")


def _adb_root(retries: int = 3, retry_delay_s: float = 2.0):
    """Runs `adb root`, retrying on transient failure. Right after adbd is
    freshly enabled (systemctl start android-tools-adbd), the device can
    show up in `adb devices` as "device" a moment before adbd is actually
    ready to accept a root-escalation request -- so the first `adb root`
    issued right after wait_for_adb_device() returns can race and fail with
    exit code 1, even though the exact same command succeeds a couple
    seconds later (confirmed live: a manual retry worked immediately).
    Jittering the retry delay (rather than a fixed 2s) spreads retries
    across that race window instead of landing in it the same way every
    run."""
    last_result = None
    for attempt in range(1, retries + 1):
        result = subprocess.run(["adb", "root"], capture_output=True, text=True)
        if result.returncode == 0:
            return
        last_result = result
        if attempt < retries:
            delay = _jittered_delay(retry_delay_s)
            print(f"'adb root' attempt {attempt}/{retries} failed; retrying in {delay:.1f}s ...")
            time.sleep(delay)
    raise RuntimeError(
        f"'adb root' failed after {retries} attempts (rc={last_result.returncode}): "
        f"{last_result.stderr.strip() or last_result.stdout.strip()}"
    )


def adb_pull_logs(local_dir: Path, log_names: list):
    """Pulls /data/Logs-<name> for every name in log_names (e.g.
    ["default-1", "default-2", "default-3", "debug-1", ...]) into
    local_dir in a single `adb pull`. `adb root` first -- /data/Logs-* is
    only readable as root on this device -- which restarts adbd, so we
    re-wait for the device to come back before pulling."""
    local_dir.mkdir(parents=True, exist_ok=True)

    print("Restarting adb as root ...")
    _adb_root()
    wait_for_adb_device()

    remote_dirs = [f"/data/Logs-{name}" for name in log_names]
    print(f"Pulling {', '.join(remote_dirs)} -> {local_dir} ...")
    subprocess.run(["adb", "pull", *remote_dirs, str(local_dir)], check=True)


def _read_pulled_file(local_dir: Path, log_name: str, filename: str) -> str:
    path = local_dir / f"Logs-{log_name}" / filename
    if not path.exists():
        raise RuntimeError(f"Expected pulled log file not found: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


def read_pulled_logs(local_dir: Path, log_name: str) -> dict:
    """Reads the collect_boot_logs.sh outputs for one boot out of
    local_dir/Logs-<log_name>/, already pulled from the device via
    adb_pull_logs. Maps to the same text arguments build_run() needs."""
    return {
        "sat_text": _read_pulled_file(local_dir, log_name, "systemd_analyze.txt"),
        "cc_text": _read_pulled_file(local_dir, log_name, "systemd-analyze_critical_chain.txt"),
        "cc_sysinit_text": _read_pulled_file(local_dir, log_name, "systemd-analyze_critical_chain_sysinit.txt"),
        "blame_text": _read_pulled_file(local_dir, log_name, "blame_systemd.txt"),
        "dmesg_text": _read_pulled_file(local_dir, log_name, "dmesg.txt"),
        "journalctl_text": _read_pulled_file(local_dir, log_name, "journalctl.log"),
        "systemctl_text": _read_pulled_file(local_dir, log_name, "blame_systemdsystemctl.txt"),
    }


def reboot_and_relogin(com_port: str, timeout_s: int):
    """Power-cycles the device via the Alpaca TAC (off, pause, on) and logs
    back in over serial -- same TAC primitive used everywhere else in this
    module. Avoids `systemctl reboot` over the console entirely: a hardware
    power cycle boots the device the same way every time, like the very
    first boot, rather than depending on in-OS reboot timing. Returns (ser,
    com_port_used); the COM port is re-probed from scratch if the original
    one doesn't come back (untested whether the debug-board UART bridge
    stays on the same port across a power cycle)."""
    print("Power-cycling device via Alpaca TAC (off, pause, on) ...")
    power_cycle_device()

    print(f"Waiting up to {timeout_s}s for device to come back online on {com_port} ...")
    try:
        ser, used_port = open_console(com_port, boot_timeout=timeout_s)
        return ser, used_port
    except RuntimeError as e:
        print(f"No login prompt on {com_port} ({e}); re-scanning all serial ports ...")
        ser, used_port = open_console(None, boot_timeout=timeout_s)
        return ser, used_port


def pull_and_record(target: str, build_path: str, num_boots: int = 3, device_info: dict = None,
                     ssh_conn: "SshConn" = None, num_previous_builds: int = None) -> Path:
    """Pulls the already-collected /data/Logs-default-1..N and
    /data/Logs-debug-1..N off the device (the device already rebooted past
    the last boot's capture) and builds/records the JSON+HTML report.
    Split out from capture_and_record() so a run that reaches this point
    but then fails (e.g. a transient `adb root` error) can be resumed with
    `--resume-pull` instead of repeating all the boots.
    device_info (from collect_device_info(), captured earlier while the
    console/SSH connection was still open) is merged with the serial/id
    below; --resume-pull never has a live connection to collect it from, so
    a resumed run's entry has only "serial" populated and shows "–" for
    machine/soc/os/kernel in its column of the Device Information table.

    When ssh_conn is given, logs are pulled via SFTP instead of adb, and
    the report's Serial field is set to "user@host" (ssh_conn is closed
    once pulling is done). Otherwise this is exactly today's adb path --
    unchanged -- and the Serial field is `adb devices`' own device id (the
    code adb/fastboot actually address this device by).

    num_previous_builds controls how many previously recorded builds this
    run's previous_build_regressions analysis compares against -- None
    (default) means ALL of them; a positive N means only the most recent
    N (see regression_analysis.detect_previous_build_regressions)."""
    device_info = dict(device_info) if device_info else {}
    local_dir = report.boot_logs_run_dir(target, build_path)
    log_names = [f"default-{i + 1}" for i in range(num_boots)] + [f"debug-{i + 1}" for i in range(num_boots)]

    if ssh_conn is not None:
        device_info["serial"] = f"{ssh_conn.user}@{ssh_conn.host}"
        print(f"Pulling /data/Logs-* -> {local_dir} via SFTP ...")
        try:
            for log_name in log_names:
                ssh_conn.pull_dir(f"/data/Logs-{log_name}", local_dir / f"Logs-{log_name}")
        finally:
            ssh_conn.close()
    else:
        print("Waiting for adb device ...")
        device_info["serial"] = wait_for_adb_device()
        adb_pull_logs(local_dir, log_names)

    boots = []
    for log_name in log_names:
        phase, n = log_name.rsplit("-", 1)
        texts = read_pulled_logs(local_dir, log_name)
        boot = build_run(
            target, build_path,
            texts["sat_text"], texts["cc_text"], texts["cc_sysinit_text"],
            texts["blame_text"], texts["dmesg_text"], texts["journalctl_text"],
            texts["systemctl_text"],
        )
        boot["phase"] = phase
        boot["log_name"] = log_name
        boots.append(boot)
        txt_path = report.save_run_backup(target, boot)
        print(f"Backed up boot {log_name} -> {txt_path}")

    # Only the first boot of each phase is recorded/rendered into the
    # JSON+HTML report (keeps the report compact); every boot is still
    # pulled, parsed, and permanently backed up above regardless of
    # num_boots.
    displayed_boots = [boots[0], boots[num_boots]]

    data_path, html_path = report._data_html_paths(target)
    data = report.load_data(data_path)

    # regression_analysis needs the PRIOR history (data["runs"] before this
    # run is appended below) to compare this run's displayed boot-1s against
    # the previous build's -- report.add_run() mutates data["runs"] in place,
    # so this must run first or the comparison would be against itself.
    analysis = regression_analysis.build_analysis(
        all_boots=boots, displayed_boots=displayed_boots, history_runs=data.get("runs", []),
        num_previous_builds=num_previous_builds,
    )
    for boot in displayed_boots:
        boot["regression_analysis"] = analysis
        boot["overall_overheads_text"] = (
            "Not yet analyzed — ask an agent to review the regression_analysis data above."
        )
        boot["optimization_possibilities"] = (
            "Not yet analyzed — ask an agent to review the overheads above and suggest fixes."
        )

    entry = {
        "timestamp": displayed_boots[0]["timestamp"],
        "build_path": build_path,
        "boots": displayed_boots,
        "device_info": device_info,
    }

    if not data.get("runs"):
        data["device"] = target
    report.add_run(data, entry)
    report.save_data(data, data_path)
    report.render(data_path, html_path)

    print(f"\nRecorded entry '{entry['timestamp']}' with {len(displayed_boots)} boot(s) displayed "
          f"({len(boots)} captured total, {len(data['runs'])} total entries) -> {data_path}")
    print(f"Report regenerated -> {html_path}")

    return html_path


def capture_and_record(target: str = None, build_path: str = None,
                        com_port: str = None, boot_timeout: int = 480,
                        num_boots: int = 3, num_previous_builds: int = None,
                        host: str = None, user: str = "root", password: str = None,
                        key_file: str = None, ssh_port: int = 22) -> Path:
    """Runs the full capture: connect, detect target/build label, collect
    device info, run the boot loop (default-cmdline boots then
    debug-cmdline boots, power-cycling/rebooting between each), then pull
    logs and record the report. host selects SSH mode (SshConn, SSH soft
    reboot between boots, SFTP pull); otherwise this is serial-console mode
    (SerialConn, Alpaca TAC power-cycle, adb pull) -- unchanged from
    before SSH support was added."""
    if host:
        print(f"Connecting to {user}@{host}:{ssh_port} over SSH ...")
        conn = SshConn(host, user, password, key_file, ssh_port)
        print("Connected.")
    else:
        print("Logging in over serial console" + (f" on {com_port}" if com_port else " (auto-detecting port)") + " ...")
        ser, com_port = open_console(com_port, boot_timeout=boot_timeout)
        conn = SerialConn(ser)
        print(f"Logged in via {com_port}")

    if target is None:
        target = detect_target(conn)
        print(f"Detected target: {target}")

    if build_path is None:
        build_path = next_build_label(target)
        print(f"No --build-path given; using auto-generated build label: {build_path}")

    print("Collecting device info ...")
    device_info = collect_device_info(conn)

    boot_units = [("default", i + 1) for i in range(num_boots)] + \
                 [("debug", i + 1) for i in range(num_boots)]

    for idx, (phase, n) in enumerate(boot_units):
        if idx > 0:
            if phase == "debug" and n == 1:
                print("Adding debug kernel cmdline params ...")
                ensure_debug_cmdline_params(conn)
            if host:
                conn = conn.reboot_and_wait(boot_timeout)
            else:
                conn.close()
                ser, com_port = reboot_and_relogin(com_port, boot_timeout)
                conn = SerialConn(ser)
        log_name = f"{phase}-{n}"
        print(f"\n--- Boot {log_name} ({idx + 1}/{len(boot_units)}) ---")
        collect_boot_on_device(conn, log_name)

    if host:
        return pull_and_record(target, build_path, num_boots, device_info=device_info, ssh_conn=conn,
                                num_previous_builds=num_previous_builds)

    enable_adb(conn)
    conn.close()
    return pull_and_record(target, build_path, num_boots, device_info=device_info,
                            num_previous_builds=num_previous_builds)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_capture(args):
    """Runs (or resumes) boot-time capture against an already-flashed
    device. No flashing happens here or anywhere in this module."""
    if args.host and (args.com_port or args.tac_port):
        print("--host (SSH mode) and --com-port/--tac-port (serial mode) are mutually exclusive; pass one or the other.")
        sys.exit(1)

    global TAC_PORT_NAME
    TAC_PORT_NAME = args.tac_port

    if args.num_boots < 1:
        print(f"--num-boots must be >= 1 (got {args.num_boots}); defaulting to 3.")
        args.num_boots = 3

    password = args.password
    if args.host and not args.key and not password:
        import getpass
        password = getpass.getpass(f"SSH password for {args.user}@{args.host}: ")

    if args.resume_pull:
        if not args.target:
            print("--resume-pull requires --target (no console/SSH session is opened to auto-detect it).")
            sys.exit(1)
        if not args.build_path:
            print("--resume-pull requires --build-path.")
            sys.exit(1)
        print("--resume-pull: skipping login and the boot loop; pulling already-collected logs ...")
        try:
            if args.host:
                ssh_conn = SshConn(args.host, args.user, password, args.key, args.ssh_port)
                pull_and_record(args.target, args.build_path, args.num_boots, ssh_conn=ssh_conn,
                                 num_previous_builds=args.compare_builds)
            else:
                pull_and_record(args.target, args.build_path, args.num_boots,
                                 num_previous_builds=args.compare_builds)
        except RuntimeError as e:
            print(f"\n{e}")
            sys.exit(1)
        return

    print(f"\nCapturing boot-time data over {'SSH' if args.host else 'serial console'}...")
    try:
        capture_and_record(
            target=args.target,
            build_path=args.build_path,
            com_port=args.com_port,
            boot_timeout=args.boot_timeout,
            num_boots=args.num_boots,
            num_previous_builds=args.compare_builds,
            host=args.host,
            user=args.user,
            password=password,
            key_file=args.key,
            ssh_port=args.ssh_port,
        )
    except RuntimeError as e:
        print(f"\nBoot-time capture failed: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nBoot-time capture aborted by user; no run was recorded.")
        sys.exit(1)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="capture.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--target", help="Override/declare target name (e.g. iq-9075-evk). If omitted, auto-detected via serial console/SSH (uname -n) -- unless --resume-pull is given, which requires it explicitly.")
    parser.add_argument("--com-port", help="Serial console COM port, used for login/detection (auto-detected by probing all ports if omitted). Mutually exclusive with --host.")
    parser.add_argument("--boot-timeout", type=int, default=480, help="Seconds to wait for a login prompt (serial) or for the device to come back (SSH), on first login and after each reboot (default: 480)")
    parser.add_argument("--tac-port", help="Alpaca TAC COM port name, e.g. VTP8 (auto-detected if only one TAC device), used only to power-cycle the device between boots in serial mode. Mutually exclusive with --host.")
    parser.add_argument("--build-path", help='Build label to record this capture under, e.g. "Build-7" (defaults to the next auto-incrementing "Build-<N>" label for this target, unless --resume-pull is given, which requires it explicitly)')
    parser.add_argument("--num-boots", type=int, default=3, help="Number of consecutive boots to capture PER PHASE -- e.g. 3 (the default) captures 3 default-cmdline boots then 3 debug-cmdline boots (6 total). Values < 1 fall back to 3.")
    parser.add_argument("--compare-builds", type=int, default=None, help="Number of previously recorded builds to compare this run against for previous_build_regressions (e.g. 3 compares the current build against its 3 immediately preceding builds). Defaults to ALL previously recorded builds. If fewer than N prior builds exist, compares against however many do.")
    parser.add_argument("--resume-pull", action="store_true", help="Skip login/boot-loop entirely: adb is already enabled (serial mode) or the device is already reachable (SSH mode), and all boots' /data/Logs-<n> are already on the device (a prior run reached the pull step and failed partway) -- just pull logs and (re)build the report. Requires --target and --build-path.")
    parser.add_argument("--host", help="SSH hostname/IP of the device. Presence selects SSH mode instead of serial console; power-cycling between boots is then done via a soft `reboot` over SSH instead of the Alpaca TAC, and logs are pulled via SFTP instead of adb. Mutually exclusive with --com-port/--tac-port. Requires: pip install paramiko")
    parser.add_argument("--user", default="root", help="SSH username (default: root)")
    parser.add_argument("--password", help="SSH password. If omitted and --key is not given, prompted interactively when --host is used.")
    parser.add_argument("--key", help="Path to an SSH private key file, used instead of a password")
    parser.add_argument("--ssh-port", type=int, default=22, help="SSH port (default: 22)")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    cmd_capture(args)


if __name__ == "__main__":
    main()
