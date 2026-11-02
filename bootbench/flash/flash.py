#!/usr/bin/env python3
r"""
flash.py -- EDL flashing, Alpaca TAC power/EDL control, serial console
login/device detection, and PCAT device discovery/flash for a Qualcomm
target device.

Finds the latest nightly Yocto build, detects the target name and on-board
memory type (UFS vs EMMC) over the serial console (unless overridden via
--target/--memory-type), enters EDL mode, and flashes via PCAT.

Unlike a plain fixed-sequence automation script, every retry/poll loop in
this module uses randomized (jittered) backoff and a shuffled probe order
instead of a fixed sleep interval and a fixed scan order. Two runs against
the same flaky hardware will not retry/poll in lockstep with each other --
this is deliberate: a fixed interval means every retry across every run
collides with the same transient window (USB re-enumeration, TAC COM churn)
in the same way, while a jittered one spreads attempts out and converges
faster on average. The documented *outcome* (flags, workflow steps, return
value, error messages) is unchanged from the fixed-interval version this
replaces -- only the internal timing/order is non-deterministic.

Run with the ARM64 Python launcher on this machine (`py -3`), or with a
regular Windows Python install (`python3`):
    py -3 flash.py [--target iq-9075-evk] [--com-port COM40] [--yes]
    python3 flash.py --all --yes

--all additionally runs capture.py then report.py in-process, right after a
successful flash, using the target/build-path/com-port this flash just
resolved -- equivalent to bootbench.py's `flash capture report` (or `all`).

Requires: pip install comtypes pyserial
"""

from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

def _ensure_importable(module_name: str) -> None:
    """Make `<module_name>.py` importable regardless of which of this
    skill tree's two layouts it's actually sitting in:
      - flat:        flash.py / capture.py / report.py side by side (this repo)
      - per-usecase: .../flash/flash.py, .../capture/capture.py,
        .../report/report.py as separate sibling skill folders (the layout
        actually seen in the installed `bootbench` skill tree)
    flash, capture, and report are meant to be run as independent stages --
    this only exists so the few places that still need to reach into another
    stage's module (e.g. --all, or capture.py's use of flash.py/report.py)
    keep working no matter which layout they're deployed under, without
    requiring real packaging just for that.
    """
    here = Path(__file__).resolve().parent
    for candidate in (here, here.parent / module_name):
        if (candidate / f"{module_name}.py").is_file() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
            return


EPILOG = r"""
examples:
  Flash only -- waits for a manual y/N confirmation right before flashing:
      py -3 flash.py

  Flash, then capture + report in one command (skip the y/N prompt with --yes):
      py -3 flash.py --all --yes

  Recover a device stuck in EDL/Sahara mode:
      py -3 flash.py --recover
"""

# =============================================================================
# EDL flashing: Alpaca TAC power/EDL control, serial console login, and PCAT
# device discovery/flash.
# =============================================================================

YOCTO_SHARE = r"\\swayam\QLI_Builds\Yocto"
PERFORMANCE_SUBDIR = "performance"
BUILD_FOLDER_PREFIX = "qcom-multimedia-proprietary-image"
BUILD_NAME_RE = re.compile(r"_Nightly_Build_master_(\d+)$")

PCAT_EXE = r"C:\Program Files (x86)\Qualcomm\PCAT\bin\PCAT.exe"

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
# the result (confirmed live: corrupted a build-dir Path built from `uname -n`
# output). Always stripped in run_serial_command, regardless of strip_noise --
# unlike kernel log noise, it's never legitimate command payload (e.g. dmesg
# would never emit this OSC format itself).
OSC_SEQUENCE_RE = re.compile(r"\x1b\]\d+;.*?\x1b\\", re.DOTALL)


def _strip_kernel_log_noise(text: str) -> str:
    return KERNEL_LOG_NOISE_RE.sub("", text)


def _strip_osc_sequences(text: str) -> str:
    return OSC_SEQUENCE_RE.sub("", text)

# Fallback default for open_console() when no explicit boot_timeout is given
# (e.g. --recover doesn't open a console at all). Callers that reboot the
# device first (pre-flash target detection, post-flash boot capture) pass
# the longer --boot-timeout explicitly instead of relying on this.
PRE_FLASH_LOGIN_TIMEOUT_S = 60

# Jitter bounds for the retry/poll loops below (_open_tac, wait_for_edl_device,
# find_console_port). A fixed sleep(N) makes every retry across every run land
# on the exact same wall-clock offset from the triggering event (BootToEDL,
# power-cycle, TAC open) -- if that offset consistently lands inside a
# transient busy window (USB re-enumeration, TAC COM handle churn), every run
# retries into the same wall and the loop's own timeout is what eventually
# saves it, not the retry. Jitter spreads attempts across the window instead.
JITTER_MIN_FACTOR = 0.5
JITTER_MAX_FACTOR = 1.5


def _jittered_delay(base_delay_s: float) -> float:
    return random.uniform(base_delay_s * JITTER_MIN_FACTOR, base_delay_s * JITTER_MAX_FACTOR)


TAC_PORT_NAME = None  # auto-detected from the single connected TAC device if None
_tac_server_ref = None  # keeps the AlpacaTACServer COM object alive; see _open_tac()


def _run_powershell(command: str) -> str:
    """Runs a PowerShell command and returns stdout. Raises RuntimeError with
    PowerShell's own stderr on failure -- subprocess.run(check=True)'s
    CalledProcessError alone doesn't surface stderr, so a share access/
    permissions/connectivity error (e.g. \\\\swayam not reachable from this
    machine) shows up as a bare non-zero exit code with no explanation."""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"PowerShell command failed (exit {result.returncode}): {command}\n"
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def find_latest_build(share_root: str) -> Path:
    stdout = _run_powershell(
        f"Get-ChildItem -LiteralPath '{share_root}' -Directory "
        f"| Select-Object -ExpandProperty Name"
    )
    candidates = []
    for name in stdout.splitlines():
        name = name.strip()
        m = BUILD_NAME_RE.search(name)
        if m:
            candidates.append((int(m.group(1)), name))
    if not candidates:
        raise RuntimeError(f"No '_Nightly_Build_master_<N>' folders found under {share_root}")
    candidates.sort()
    _, latest_name = candidates[-1]
    return Path(share_root) / latest_name


def resolve_build_dir(latest_build: Path, target: str) -> Path:
    build_dir = latest_build / PERFORMANCE_SUBDIR / f"{BUILD_FOLDER_PREFIX}-{target}"
    stdout = _run_powershell(f"Test-Path -LiteralPath '{build_dir}\\rawprogram0.xml'")
    if stdout.strip() != "True":
        raise RuntimeError(
            f"Build folder does not look like a flat build (missing rawprogram0.xml): {build_dir}"
        )
    return build_dir


# ---------------------------------------------------------------------------
# Serial console -- login, target detection, and (via capture.py) post-flash
# boot-time data collection all go over this. See open_console().
# ---------------------------------------------------------------------------

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


def find_console_port(timeout_s: int = PRE_FLASH_LOGIN_TIMEOUT_S) -> str:
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
    newline at a jittered interval (first boot after flash can take minutes;
    a fixed nudge interval risks always landing mid-boot-message-burst on a
    device that logs verbosely at a steady cadence, drowning the nudge)."""
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


def open_console(com_port: str = None, boot_timeout: int = PRE_FLASH_LOGIN_TIMEOUT_S):
    """Opens the serial console, waits for a login/shell prompt, and logs in.
    Returns (ser, com_port_used); caller is responsible for closing ser."""
    import serial

    if com_port is None:
        com_port = find_console_port(timeout_s=boot_timeout)

    ser = serial.Serial(com_port, SERIAL_BAUD, timeout=1)
    login(ser, boot_timeout)
    return ser, com_port


def detect_target_via_serial(ser) -> str:
    target = run_serial_command(ser, "uname -n")
    if not target:
        raise RuntimeError("Could not determine target name from 'uname -n' (empty output)")
    return target


# Top-level block device names, matched at line start since lsblk's tree
# branch characters (|-, `-) only prefix partition lines, never the
# top-level disk entry itself -- e.g. "sda       8:0 ..." vs "|-sda1    8:1 ...".
LSBLK_EMMC_RE = re.compile(r"(?m)^mmcblk\d+")
LSBLK_UFS_RE = re.compile(r"(?m)^sd[a-z]\d*\s")


def detect_memory_type_via_serial(ser) -> str:
    """Runs `lsblk` over the serial console and classifies the device's
    on-board storage as EMMC (top-level mmcblk0/mmcblk0boot0/mmcblk0boot1
    nodes) or UFS (top-level sda/sdb/... SCSI disk nodes) -- there's no
    sysfs node that reports this directly, and PCAT needs the right
    -MEMORYTYPE to flash successfully."""
    output = run_serial_command(ser, "lsblk")
    if LSBLK_EMMC_RE.search(output):
        return "EMMC"
    if LSBLK_UFS_RE.search(output):
        return "UFS"
    raise RuntimeError(f"Could not determine memory type (UFS vs EMMC) from 'lsblk' output:\n{output}")


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


def _try_serial_command(ser, cmd: str) -> str | None:
    """Like run_serial_command, but returns None instead of raising when the
    command fails -- e.g. a sysfs node (soc0/revision) that doesn't exist on
    every device/kernel. Used for best-effort device-info fields that
    shouldn't abort a capture over one missing field."""
    try:
        return run_serial_command(ser, cmd)
    except RuntimeError:
        return None


def collect_device_info(ser) -> dict:
    """Collects device identification fields over the serial console for the
    report's Device Information panel: Machine (device-tree model), SoC
    (soc0 machine + revision), OS (/etc/os-release), Kernel (uname -r), and
    Captured (when this snapshot was taken). Best-effort per field -- a
    missing one is simply omitted from the rendered panel rather than
    aborting the capture.

    Serial is deliberately NOT collected here. soc0/serial_number is the
    SoC's own serial, which is not the id this device is actually addressed
    by over adb -- capturing that instead reports a number that doesn't let
    anyone find the device via `adb devices`/`adb -s <serial>`. The report's
    Serial field is populated by capture.py from `adb devices`'s own device
    id, once adb is up (see capture.py's wait_for_adb_device)."""
    model = _try_serial_command(ser, "cat /proc/device-tree/model")
    machine = _try_serial_command(ser, "cat /sys/devices/soc0/machine")
    revision = _try_serial_command(ser, "cat /sys/devices/soc0/revision")
    kernel = _try_serial_command(ser, "uname -r")
    os_release_text = _try_serial_command(ser, "cat /etc/os-release")

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
# Alpaca TAC (EDL entry / power control)
# ---------------------------------------------------------------------------

def _open_tac(retries: int = 3, retry_delay_s: float = 2.0):
    import comtypes.client as cc

    # Keep a reference to the top-level AlpacaTACServer COM object at module scope.
    # Create_TAC_Server() returns a *child* COM object (ITACServer); if nothing
    # keeps the parent alive, Python garbage-collects it as soon as this function
    # returns, which disconnects the child's interface and makes every subsequent
    # call (e.g. BootToEDLButton, Close) fail with "The object invoked has
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


def enter_edl_mode():
    tac = _open_tac()
    try:
        print("Triggering BootToEDL...")
        tac.BootToEDLButton()
    finally:
        tac.Close()


def power_cycle_device():
    """Recover a device stuck in EDL/Sahara (or abort a flash) by power-cycling it
    back to normal boot. The Alpaca TAC has no single 'reset' command -- only
    discrete PowerOffButton / PowerOnButton -- so recovery is off, pause, on."""
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
# PCAT
# ---------------------------------------------------------------------------

def pcat_list_devices() -> list:
    with tempfile.TemporaryDirectory() as tmp:
        out_file = str(Path(tmp) / "devices.json")
        result = subprocess.run(
            [PCAT_EXE, "-DEVICES", "-JSON", "TRUE", "-OUT", out_file],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"PCAT -DEVICES failed (exit {result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        data = json.loads(Path(out_file).read_text(encoding="utf-8-sig"))
    return data


def wait_for_edl_device(timeout_s: int = 90, poll_interval_s: int = 2) -> dict:
    """Polls PCAT -DEVICES until an EDL-capable device shows up. Right after
    BootToEDL, the device is still re-enumerating over USB, and PCAT -DEVICES
    can transiently fail outright (non-zero exit, not just an empty device
    list) during that window -- tolerate that like any other "not there yet"
    result instead of letting it abort the whole poll loop. A single PCAT
    -DEVICES call has been observed taking ~12s on its own (it does its own
    USB device-manager scan), so the default timeout allows for several
    genuine retries rather than 1-2.

    Each retry's wait is jittered rather than a fixed poll_interval_s -- a
    fixed interval means every poll lands at the same offset into PCAT's own
    ~12s scan cycle on every run, which can systematically miss a device that
    only appears for a narrow window inside that cycle."""
    deadline = time.time() + timeout_s
    attempt = 0
    last_devices = []
    last_error = None
    while time.time() < deadline:
        attempt += 1
        try:
            devices = pcat_list_devices()
        except RuntimeError as e:
            last_error = e
            delay = _jittered_delay(poll_interval_s)
            print(f"  PCAT -DEVICES attempt {attempt} failed ({e}); retrying in {delay:.1f}s...")
            time.sleep(delay)
            continue
        last_devices = devices
        candidates = [
            d for d in devices
            if d.get("device_state", "").upper() not in ("NORMAL",) or d.get("device_type")
        ]
        if candidates:
            return candidates[0]
        delay = _jittered_delay(poll_interval_s)
        print(f"  PCAT -DEVICES attempt {attempt}: no EDL device yet ({time.time() - (deadline - timeout_s):.0f}s elapsed); retrying in {delay:.1f}s ...")
        time.sleep(delay)
    raise RuntimeError(
        f"No EDL-capable device found via PCAT -DEVICES after {timeout_s}s. "
        f"Last seen: {last_devices}"
        + (f"; last error: {last_error}" if last_error else "")
    )


def pcat_device_id(device: dict) -> str:
    """PCAT's -DEVICES JSON sometimes reports id as the literal string "NA"
    (seen in EDL state on this board) instead of a real identifier -- fall
    back to serial_number, which PCAT does accept as -DEVICE, in that case."""
    device_id = device.get("id")
    if device_id and device_id.upper() != "NA":
        return device_id
    serial_number = device.get("serial_number")
    if serial_number:
        return serial_number
    raise RuntimeError(f"No usable device id or serial_number in PCAT device entry: {device}")


def run_pcat_flash(device_id: str, build_dir: Path, memory_type: str):
    cmd = [
        PCAT_EXE, "-PLUGIN", "SD",
        "-DEVICE", device_id,
        "-BUILD", str(build_dir),
        "-MEMORYTYPE", memory_type,
        "-SLOT", "0",
    ]
    print("\nRunning:", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    print("(Ctrl+C aborts the flash and power-cycles the device back to normal boot)")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for line in proc.stdout:
            print(line, end="")
        proc.wait()
    except KeyboardInterrupt:
        print("\nAborting flash: killing PCAT...")
        proc.kill()
        proc.wait()
        print("PCAT process killed. Power-cycling device via TAC...")
        try:
            power_cycle_device()
        except Exception as e:
            raise RuntimeError(
                f"Flash aborted (PCAT killed), but power-cycling the device via TAC also "
                f"failed: {e!r}. The device may still be in EDL/Sahara mode -- run "
                f"'py -3 flash.py --recover' to retry, or use the TAC app / physical "
                f"reset directly."
            )
        raise RuntimeError("Flash aborted by user (Ctrl+C); device power-cycled back to normal boot.")

    if proc.returncode != 0:
        raise RuntimeError(f"PCAT flash failed with exit code {proc.returncode}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_flash(args):
    """Runs build-discovery -> EDL entry -> confirm -> PCAT flash. Returns a
    dict of {target, build_path, com_port} for capture.py to reuse (--all),
    or None if the pipeline should stop here (--recover, --dry-run, or the
    user declined the confirmation prompt)."""
    global TAC_PORT_NAME
    TAC_PORT_NAME = args.tac_port

    if args.recover:
        print("Recovering device: power-cycling via TAC...")
        power_cycle_device()
        print("Done. Device should re-boot normally.")
        return None

    print(f"Finding latest nightly build under {YOCTO_SHARE} ...")
    latest_build = find_latest_build(YOCTO_SHARE)
    print(f"Latest build: {latest_build.name}")

    used_com_port = args.com_port

    if args.target and args.memory_type:
        target = args.target
        memory_type = args.memory_type
        print(f"Using target override: {target}")
        print(f"Using memory type override: {memory_type}")
    else:
        print("Power-cycling device via Alpaca TAC before target/memory-type detection (known-good boot state) ...")
        power_cycle_device()

        print("Logging in over serial console" + (f" on {args.com_port}" if args.com_port else " (auto-detecting port)") + " ...")
        ser, used_com_port = open_console(args.com_port, boot_timeout=args.boot_timeout)
        try:
            target = args.target or detect_target_via_serial(ser)
            if not args.target:
                print(f"Detected target: {target} (via {used_com_port})")

            memory_type = args.memory_type or detect_memory_type_via_serial(ser)
            if not args.memory_type:
                print(f"Detected memory type: {memory_type} (via lsblk)")
        finally:
            ser.close()

    build_dir = resolve_build_dir(latest_build, target)
    print(f"Resolved build directory: {build_dir}")

    if args.dry_run:
        print("\n--dry-run: stopping before touching the device.")
        print(f"Would run: {PCAT_EXE} -PLUGIN SD -DEVICE <discovered-after-edl> "
              f'-BUILD "{build_dir}" -MEMORYTYPE {memory_type} -SLOT 0')
        return None

    enter_edl_mode() if not args.skip_edl else print("Skipping EDL trigger; assuming device is already in EDL mode.")

    print("Waiting for device to re-enumerate in EDL mode ...")
    time.sleep(5)
    edl_device = wait_for_edl_device()
    device_id = pcat_device_id(edl_device)
    print(f"Found EDL device: {edl_device}")

    print("\nAbout to flash:")
    print(f"  Device ID:    {device_id}")
    print(f"  Build dir:    {build_dir}")
    print(f"  Memory type:  {memory_type}")
    print(f"  Slot:         0")

    if not args.yes:
        reply = input("\nProceed with flashing? [y/N] ").strip().lower()
        if reply != "y":
            print("Aborted.")
            return None

    try:
        run_pcat_flash(device_id, build_dir, memory_type)
    except RuntimeError as e:
        print(f"\n{e}")
        sys.exit(1)
    print("\nFlash completed successfully.")

    return {
        "target": target,
        "build_path": str(latest_build / PERFORMANCE_SUBDIR),
        "com_port": used_com_port,
    }


def build_parser():
    parser = argparse.ArgumentParser(
        prog="flash.py", description=__doc__, epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--target", help="Override/declare target name (e.g. iq-9075-evk). Auto-detected via serial console (uname -n) otherwise.")
    parser.add_argument("--com-port", help="Serial console COM port, used for login/detection (auto-detected by probing all ports if omitted)")
    parser.add_argument("--boot-timeout", type=int, default=480, help="Seconds to wait for a login prompt, on first login and after each reboot (default: 480)")
    parser.add_argument("--tac-port", help="Alpaca TAC COM port name, e.g. VTP8 (auto-detected if only one TAC device)")
    parser.add_argument("--memory-type", choices=["UFS", "EMMC"], help="Override/declare the on-board storage type passed to PCAT's -MEMORYTYPE (auto-detected via 'lsblk' over serial console if omitted)")
    parser.add_argument("--dry-run", action="store_true", help="Resolve paths and print the plan, then exit before touching the device (stops before EDL mode)")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt before flashing (otherwise waits for manual y/N after entering EDL mode)")
    parser.add_argument("--skip-edl", action="store_true", help="Device is already in EDL mode; skip the TAC BootToEDL step")
    parser.add_argument("--recover", action="store_true", help="Power-cycle the device via TAC (out of EDL/Sahara back to normal boot) and exit")
    parser.add_argument("--all", action="store_true", help="After a successful flash, also run capture.py then report.py in-process (equivalent to bootbench.py's 'flash capture report')")

    # Only import/merge capture.py's and report.py's own flags when --all is
    # actually on this command line. Importing them unconditionally would
    # make a bare `flash.py` (no --all) fail with ModuleNotFoundError the
    # moment the `flash` skill is deployed on its own, without capture.py/
    # report.py physically present -- flash itself never calls into either
    # module unless --all chains into them, so flash-only use shouldn't pay
    # that dependency. A plain `sys.argv` scan (rather than a conditional
    # two-pass argparse call) is enough to decide this before the parser
    # that would need those extra flags even exists.
    if "--all" in sys.argv:
        try:
            _ensure_importable("capture")
            _ensure_importable("report")
            import capture
            import report
        except ModuleNotFoundError as e:
            parser.error(
                f"--all requires capture.py and report.py alongside flash.py ({e}). "
                "Run flash.py without --all for a flash-only skill, or add capture.py/"
                "report.py back to run the full flash -> capture -> report pipeline."
            )
        capture.add_capture_args(parser)
        report.add_report_args(parser)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.recover and args.all:
        parser.error("--recover and --all cannot be combined")

    handoff = cmd_flash(args)
    if handoff is None:
        return  # --recover, --dry-run, or the user declined to confirm

    if not args.all:
        return

    _ensure_importable("capture")
    _ensure_importable("report")
    import capture
    import report

    if args.num_boots < 1:
        print(f"--num-boots must be >= 1 (got {args.num_boots}); defaulting to 3.")
        args.num_boots = 3

    print("\n--all: continuing into capture ...")
    try:
        capture.capture_and_record(
            target=handoff["target"],
            build_path=handoff["build_path"],
            com_port=handoff["com_port"],
            boot_timeout=args.boot_timeout,
            num_boots=args.num_boots,
        )
    except RuntimeError as e:
        print(f"\nBoot-time capture failed: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nBoot-time capture aborted by user; no run was recorded.")
        sys.exit(1)
    # capture.capture_and_record already records + renders the report, so no
    # separate report.py call is needed here -- matching bootbench.py's own
    # 'flash capture report' / 'all' behavior (report only runs standalone
    # when capture didn't already run in the same invocation).


if __name__ == "__main__":
    main()
