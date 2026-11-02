#!/usr/bin/env python3
r"""
capture.py -- boot-time capture + log parsing for a Qualcomm target device.

This is the entry point for the `capture` skill usecase (see
`../capture/SKILL.md`): boot an already-flashed device N times under the
default kernel cmdline, then N more with debug cmdline params added, pull
all collected logs off the device via `adb`, parse them into structured
metrics, and record + render the report (via report.py).

Like flash.py (see its own module docstring), the retry/poll loops below
use randomized (jittered) backoff instead of a fixed sleep interval -- an
`adb root` issued the instant the device shows up in `adb devices` can race
adbd's own startup and fail (confirmed live: an identical manual retry
succeeded a moment later), and a fixed retry delay means every retry across
every run lands on that same race window the same way. Jitter spreads
retries across the window instead of repeating the same losing timing.
Everything else here -- flags, parsing, the report handoff -- is unchanged.

Run standalone against an already-flashed device:
    py -3 capture.py --build-path "\\swayam\...\performance"

If --build-path is omitted, the latest nightly build under flash.YOCTO_SHARE
is resolved automatically (same discovery flash.py itself uses) -- this is
new behavior vs. bootbench.py's `capture`, which required --build-path
explicitly whenever it ran without `flash` in the same command. --resume-pull
still requires --build-path explicitly (recovering a specific already-flashed
build; silently defaulting to "latest" could pull the wrong build's logs).

Run with the ARM64 Python launcher on this machine (`py -3`), or with a
regular Windows Python install (`python3`).
Requires: pip install pyserial  (adb also needed, on PATH)
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


def _ensure_importable(module_name: str) -> None:
    """Make `<module_name>.py` importable regardless of which of this
    skill tree's two layouts it's actually sitting in:
      - flat:        flash.py / capture.py / report.py side by side (this repo)
      - per-usecase: .../flash/flash.py, .../capture/capture.py,
        .../report/report.py as separate sibling skill folders (the layout
        actually seen in the installed `bootbench` skill tree)
    capture.py has no serial-console or report-rendering logic of its own --
    unlike flash.py's optional --all chaining, these imports are required --
    so this runs unconditionally at module load, before `import flash` /
    `import report` below, rather than being gated behind a flag.
    """
    here = Path(__file__).resolve().parent
    for candidate in (here, here.parent / module_name):
        if (candidate / f"{module_name}.py").is_file() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
            return


_ensure_importable("flash")
_ensure_importable("report")
import flash
import report
import regression_analysis

DELTA_MERGE_S = 0.05  # threshold for noting graphical.target overshoot

COLLECT_SCRIPT_REMOTE_PATH = "/data/collect_boot_logs.sh"
COLLECT_SCRIPT_HEREDOC_MARKER = "BOOT_LOG_SCRIPT_EOF"

# Jitter bounds for this module's own retry/poll loops (_adb_root,
# wait_for_adb_device) -- same rationale as flash.py's JITTER_MIN_FACTOR/
# JITTER_MAX_FACTOR, kept as a separate copy here since capture.py and
# flash.py are independently runnable entry points and neither should
# require importing the other just for this constant.
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
        "epoch_advanced": find(r"\[\s*([\d.]+)\]\s*systemd\[1\]: System time advanced to built-in epoch"),
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


def resolve_boot_entry(ser):
    """Determines the on-device bootloader entry file to edit for debug
    cmdline params, without hardcoding a path. Runs `bootctl status | grep
    type` -- if the device isn't a Boot Loader Specification Type #1 (i.e.
    Type #2 or unrecognized), there's no per-entry options line to edit, so
    returns None. Otherwise runs `bootctl status | grep conf` and pulls the
    current entry's real `source:` path out of that output. Returns the
    resolved path string, or None if not Type #1."""
    type_output = flash.run_serial_command(ser, "bootctl status | grep type")
    if "Type #1" not in type_output:
        return None

    conf_output = flash.run_serial_command(ser, "bootctl status | grep conf")
    match = BOOTCTL_SOURCE_RE.search(conf_output)
    if not match:
        raise RuntimeError(
            "bootctl status reports Boot Loader Specification Type #1, but no "
            f"'source:' line was found in its conf output to resolve the current "
            f"entry file: {conf_output!r}"
        )
    return match.group(1)


def ensure_debug_cmdline_params(ser):
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
    entry_path = resolve_boot_entry(ser)
    if entry_path is None:
        print(
            "bootctl reports Boot Loader Specification Type #2 (or unrecognized) "
            "-- no per-entry options line to edit; skipping debug kernel cmdline params."
        )
        return None

    try:
        flash.run_serial_command(ser, f"test -f {entry_path}")
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

    content = flash.run_serial_command(ser, f"cat {entry_path}", strip_noise=False)
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
    flash.run_serial_command(ser, write_cmd, timeout_s=60)
    print(f"Updated {entry_path} and synced.")
    return entry_path


def ensure_collect_script(ser):
    """Writes collect_boot_logs.sh to COLLECT_SCRIPT_REMOTE_PATH if it isn't
    already there, and chmod +x's it. Safe to call every boot -- self-healing
    if /data was ever wiped, but only actually writes once per device."""
    check = flash.run_serial_command(
        ser, f"test -f {COLLECT_SCRIPT_REMOTE_PATH} && echo EXISTS || echo MISSING"
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
    flash.run_serial_command(ser, write_cmd, timeout_s=60)


def collect_boot_on_device(ser, log_name: str):
    """Runs collect_boot_logs.sh (provisioning it first if needed), syncs,
    renames /data/Logs -> /data/Logs-<log_name> (e.g. "default-1",
    "debug-2") so it survives the next boot's run, and syncs again."""
    ensure_collect_script(ser)

    print(f"Running collect_boot_logs.sh (boot {log_name}) ...")
    flash.run_serial_command(ser, f"sh {COLLECT_SCRIPT_REMOTE_PATH}", timeout_s=180)
    flash.run_serial_command(ser, "sync")

    print(f"Renaming /data/Logs -> /data/Logs-{log_name} ...")
    flash.run_serial_command(ser, f"rm -rf /data/Logs-{log_name}; mv /data/Logs /data/Logs-{log_name}")
    flash.run_serial_command(ser, "sync")


def enable_adb(ser):
    print("Enabling adb ...")
    flash.run_serial_command(ser, "touch /etc/usb-debugging-enabled")
    flash.run_serial_command(ser, "systemctl start android-tools-adbd")


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
    back in over serial -- same TAC primitive used for --recover. Avoids
    `systemctl reboot` over the console entirely: a hardware power cycle
    boots the device the same way every time, like the very first boot after
    a flash, rather than depending on in-OS reboot timing. Returns (ser,
    com_port_used); the COM port is re-probed from scratch if the original
    one doesn't come back (untested whether the debug-board UART bridge
    stays on the same port across a power cycle). Delegates to flash.py's
    power_cycle_device()/open_console(), which already carry this module's
    own jittered-retry treatment, so no separate jitter is needed here."""
    print("Power-cycling device via Alpaca TAC (off, pause, on) ...")
    flash.power_cycle_device()

    print(f"Waiting up to {timeout_s}s for device to come back online on {com_port} ...")
    try:
        ser, used_port = flash.open_console(com_port, boot_timeout=timeout_s)
        return ser, used_port
    except RuntimeError as e:
        print(f"No login prompt on {com_port} ({e}); re-scanning all serial ports ...")
        ser, used_port = flash.open_console(None, boot_timeout=timeout_s)
        return ser, used_port


def pull_and_record(target: str, build_path: str, num_boots: int = 3, device_info: dict = None) -> Path:
    """Pulls the already-collected /data/Logs-default-1..N and
    /data/Logs-debug-1..N off the device (adb must already be enabled and
    the device already rebooted past the last boot's capture) and
    builds/records the JSON+HTML report. Split out from
    capture_and_record() so a run that reaches this point but then fails
    (e.g. a transient `adb root` error) can be resumed with
    `--resume-pull` instead of repeating the flash and all boots.
    device_info (from flash.collect_device_info(), captured earlier while
    the serial console was still open) is merged with the adb-derived
    serial below; --resume-pull never has a live console to collect it
    from, so a resumed run's entry has only "serial" populated and shows
    "–" for machine/soc/os/kernel in its column of the Device Information
    table.

    The report's Serial field is always set from here, not from
    device_info -- it's `adb devices`' own device id (the code adb/fastboot
    actually address this device by), captured fresh below regardless of
    whether a serial-console device_info snapshot is available, since adb
    being up is this function's own precondition either way."""
    print("Waiting for adb device ...")
    adb_serial = wait_for_adb_device()

    device_info = dict(device_info) if device_info else {}
    device_info["serial"] = adb_serial

    local_dir = report.boot_logs_run_dir(target, build_path)
    log_names = [f"default-{i + 1}" for i in range(num_boots)] + [f"debug-{i + 1}" for i in range(num_boots)]
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


def capture_and_record(target: str, build_path: str,
                        com_port: str = None, boot_timeout: int = 480,
                        num_boots: int = 3) -> Path:
    print("Logging in over serial console" + (f" on {com_port}" if com_port else " (auto-detecting port)") + " ...")
    ser, com_port = flash.open_console(com_port, boot_timeout=boot_timeout)
    print(f"Logged in via {com_port}")

    if target is None:
        target = flash.detect_target_via_serial(ser)
        print(f"Detected target: {target} (via {com_port})")

    print("Collecting device info ...")
    device_info = flash.collect_device_info(ser)

    boot_units = [("default", i + 1) for i in range(num_boots)] + \
                 [("debug", i + 1) for i in range(num_boots)]

    for idx, (phase, n) in enumerate(boot_units):
        if idx > 0:
            if phase == "debug" and n == 1:
                print("Adding debug kernel cmdline params ...")
                ensure_debug_cmdline_params(ser)
            ser.close()
            ser, com_port = reboot_and_relogin(com_port, boot_timeout)
        log_name = f"{phase}-{n}"
        print(f"\n--- Boot {log_name} ({idx + 1}/{len(boot_units)}) ---")
        collect_boot_on_device(ser, log_name)

    enable_adb(ser)
    ser.close()

    return pull_and_record(target, build_path, num_boots, device_info=device_info)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def add_capture_args(parser):
    """Adds capture.py's own-specific flags (not --target/--com-port/
    --boot-timeout, which flash.py/capture.py/report.py each define
    directly) to an existing parser. Shared with flash.py's --all parser so
    --build-path/--num-boots/--resume-pull aren't redefined by hand there."""
    capture_group = parser.add_argument_group("capture")
    capture_group.add_argument("--build-path", help=r'Nightly build path this boot used, e.g. "\\swayam\...\performance" (defaults to the latest nightly build if omitted, unless --resume-pull is given)')
    capture_group.add_argument("--num-boots", type=int, default=3, help="Number of consecutive boots to capture PER PHASE -- e.g. 3 (the default) captures 3 default-cmdline boots then 3 debug-cmdline boots (6 total). Values < 1 fall back to 3.")
    capture_group.add_argument("--resume-pull", action="store_true", help="Skip login/boot-loop entirely: adb is already enabled and all boots' /data/Logs-<n> are already on the device (a prior run reached adb pull and failed partway) -- just pull logs and (re)build the report. Requires --target and --build-path.")
    return parser


def cmd_capture(args, handoff=None):
    """Runs (or resumes) boot-time capture. Uses target/build_path/com_port
    from `handoff` (set when 'flash' ran in the same invocation, i.e.
    flash.py --all) in preference to --target/--build-path/--com-port."""
    if args.num_boots < 1:
        print(f"--num-boots must be >= 1 (got {args.num_boots}); defaulting to 3.")
        args.num_boots = 3

    if args.resume_pull:
        if not args.target:
            print("--resume-pull requires --target (no serial console is opened to auto-detect it).")
            sys.exit(1)
        if not args.build_path:
            print("--resume-pull requires --build-path.")
            sys.exit(1)
        print("--resume-pull: skipping login and the boot loop; pulling already-collected logs ...")
        try:
            pull_and_record(args.target, args.build_path, args.num_boots)
        except RuntimeError as e:
            print(f"\n{e}")
            sys.exit(1)
        return

    target = handoff["target"] if handoff else args.target
    build_path = handoff["build_path"] if handoff else args.build_path
    com_port = handoff["com_port"] if handoff else args.com_port

    if not build_path:
        print(f"No --build-path given; finding latest nightly build under {flash.YOCTO_SHARE} ...")
        latest_build = flash.find_latest_build(flash.YOCTO_SHARE)
        build_path = str(latest_build / flash.PERFORMANCE_SUBDIR)
        print(f"Using latest build: {build_path}")

    print("\nCapturing boot-time data over serial console...")
    try:
        capture_and_record(
            target=target,
            build_path=build_path,
            com_port=com_port,
            boot_timeout=args.boot_timeout,
            num_boots=args.num_boots,
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
    parser.add_argument("--target", help="Override/declare target name (e.g. iq-9075-evk). If omitted, auto-detected via serial console (uname -n) -- unless --resume-pull is given, which requires it explicitly.")
    parser.add_argument("--com-port", help="Serial console COM port, used for login/detection (auto-detected by probing all ports if omitted)")
    parser.add_argument("--boot-timeout", type=int, default=480, help="Seconds to wait for a login prompt, on first login and after each reboot (default: 480)")
    add_capture_args(parser)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    cmd_capture(args)


if __name__ == "__main__":
    main()
