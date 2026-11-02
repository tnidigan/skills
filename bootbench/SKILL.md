---
name: bootbench
description: Boot an already-flashed Qualcomm Linux target device N times over a serial console or SSH, pull and parse boot-time logs via adb or SFTP, and maintain a per-device JSON history + HTML boot-time report. Use when the user asks to capture/collect boot logs or boot times from a target device, summarize boot performance, or regenerate/update the boot-time HTML report. Analysis (outlier detection, regression/root-cause analysis, optimization recommendations) only runs when explicitly requested — see "Analysis is opt-in" below.
license: Proprietary
metadata:
  domain: embedded-linux-boot-benchmarking
---

# Bootbench — Capture / Report

Automates boot-time benchmarking on an **already-flashed** Qualcomm Linux
target device: boot the device N times under the default kernel cmdline,
then N more times with debug cmdline params added, pull all collected
logs off the device, parse them into structured boot-time metrics, and
record + render an HTML report.

Two transports are supported for reaching the device, chosen by which
flags are passed to `capture.py`:

- **Serial console** (default) — login over a serial (COM) console, an
  Alpaca TAC board power-cycles the device between boots, and `adb` pulls
  the collected logs.
- **SSH** (`--host`) — login over SSH instead, a soft `reboot` over SSH
  power-cycles the device between boots (no TAC needed), and SFTP pulls
  the collected logs (no `adb` needed).

These are mutually exclusive — pass `--host` for SSH, or `--com-port` (or
nothing, for auto-detection) for serial.

This skill has **no flashing capability**. It assumes the device already
has the build under test on it. There is no PCAT/EDL step and no nightly-
build-share lookup — each capture simply labels the build it ran against
with an auto-incrementing `Build-<N>` tag (`Build-1`, `Build-2`, ...), per
target, unless the caller supplies an explicit label.

The pipeline has two stages, both implemented in this one skill:

- **capture** — boot the device, collect raw logs, parse them into
  metrics, and record + render the report.
- **report** — regenerate/maintain the HTML report from the JSON history
  with no device involved at all (re-render, manually append a run, or
  attach agent-authored analysis text to a boot).

---

## Repository Structure

```
bootbench/                        ← this skill's root (installed at .claude/skills/bootbench/)
├── SKILL.md                        (agent-discoverable; this file — the ONLY SKILL.md in this tree)
├── capture/                        ← capture stage: boot the device, collect boot-time data
│   ├── scripts/
│   │   ├── capture.py                 ← entry point; serial console or SSH + adb/SFTP + Build-N labeling
│   │   └── regression_analysis.py     ← outlier/regression/high-hitter detection (pure functions)
│   └── reference/                     ← COT docs guiding agent-authored analysis text
│       ├── COT_outlier_detection.md
│       ├── COT_regression_rca.md
│       └── COT_optimization_recommendations.md
└── report/                         ← report stage: regenerate/maintain the HTML report
    ├── report.py                      ← entry point
    └── Boot-Charts/, Boot-Logs/        (generated output, created once capture/report has run)
```

`capture/scripts/capture.py` imports `report/report.py` directly (two
directories up: `.../capture/scripts/capture.py` → `.../report/report.py`)
and `capture/scripts/regression_analysis.py` (co-located in the same
`scripts/` folder, so no path shimming is needed for it). `report.py` has
no dependency on `capture.py` and runs fully standalone.

---

## Analysis is opt-in — read this before invoking either stage

Boot-log capture and boot-log **analysis** (outlier detection, intra-run
and previous-build regression detection, root-cause analysis, and
optimization recommendations) are two separate things. **Capture never
triggers analysis automatically.** Whether to perform the analysis step
depends entirely on what the user actually asked for:

- User says something like *"capture boot logs"*, *"boot the device and
  give me the numbers"*, or *"capture boot logs and provide a summary"*
  → run **capture only**. Report back:
  - the device info (`device_info`: Machine, SoC, OS, Kernel, Serial,
    Captured time) and the build label (`build_path`) this run was
    recorded under, and
  - the raw boot-time metrics (NHLOS, kernel, initramfs, sysinit_svc,
    total_sysinit, total_multiuser, any failed units) for each displayed
    boot,

  exactly as `capture.py` records them. Do **not** read the
  `regression_analysis` data it attaches to each displayed boot, do not
  write RCA/outlier/optimization prose, and do not call
  `report.py --report-cmd set-analysis`. Leave the two placeholder fields
  (`overall_overheads_text`, `optimization_possibilities`) exactly as
  `capture.py` left them ("Not yet analyzed — ...").
- User says something like *"capture boot logs, analyse them, and give me
  a summary"*, *"find outliers/regressions"*, *"do an RCA"*, or *"what can
  we optimize"* → run capture **and then** perform the full analysis
  workflow below before summarizing.
- If the request is ambiguous, default to capture-only and report the raw
  numbers — do not run analysis speculatively. If the user wants analysis
  too, they will ask, and it can be added afterward via
  `report.py --report-cmd set-analysis` without re-capturing.

This split exists so a quick "just get me boot numbers" request doesn't
pay the cost (and risk of an agent inventing unsupported conclusions) of
a full RCA pass it never asked for.

### The analysis workflow (only run when explicitly requested)

1. Run (or confirm) capture has completed — `capture.py` has already
   attached a `regression_analysis` dict (outliers,
   intra_run_regressions, previous_build_regressions, high_hitters,
   unit_appeared_once — see `capture/scripts/regression_analysis.py`'s
   module docstring for exactly what each category means) to every
   displayed boot in the latest run.
2. Read `capture/reference/COT_outlier_detection.md` and work through the
   `outliers`, `intra_run_regressions`, and `unit_appeared_once` findings
   for each displayed boot.
3. Read `capture/reference/COT_regression_rca.md` and work through
   `previous_build_regressions` plus `high_hitters` to identify root
   causes, cross-referencing the raw pulled logs under
   `report/Boot-Logs/<target>/<build-folder>/Logs-<log_name>/` when a
   finding needs more context than the structured data alone provides.
4. Read `capture/reference/COT_optimization_recommendations.md` and draft
   concrete, per-unit optimization suggestions from the overheads
   identified above.
5. Write the resulting prose to two text files (one per field) and commit
   them onto the boot via:
   ```
   py -3 report/report.py --report-cmd set-analysis --target <target> \
       --log-name <default-1-or-debug-1> \
       --overheads-file overheads.txt --optimization-file optimization.txt
   ```
   Run this once per displayed boot that needs analysis (typically
   `default-1` and `debug-1`). This both updates the JSON and
   re-renders the HTML in one step.
6. Summarize the findings and what was written to the report back to the
   user — don't just say "done," state the actual overheads and
   optimization suggestions.

---

## Capture stage

**Use when:** the device is already flashed with the build under test
and the user wants boot-time data collected from it (with or without
analysis — see above).

**Entry point:** `py -3 capture/scripts/capture.py [options]`

### Inputs

| Flag | Required | Description |
|---|---|---|
| `--target` | No | Target name (e.g. `iq-9075-evk`). Auto-detected via `uname -n` over the active connection if omitted — unless `--resume-pull` is given, which requires it explicitly (no connection is opened to auto-detect). |
| `--com-port` | No | Serial console COM port. Auto-detected by probing all enumerated ports if omitted. Mutually exclusive with `--host`. |
| `--boot-timeout` | No | Seconds to wait for a login prompt (serial) or for the device to come back (SSH), on first login and after each reboot. Default `480`. |
| `--tac-port` | No | Alpaca TAC COM port name (e.g. `VTP8`), used only to power-cycle the device between boots in **serial mode**. Auto-detected if exactly one TAC device is connected. Mutually exclusive with `--host`. |
| `--build-path` | No | Build label to record this capture under (e.g. `Build-7`). Defaults to the next auto-incrementing `Build-<N>` label for this target (see "Build labeling" below) — unless `--resume-pull` is given, which requires it explicitly. |
| `--num-boots` | No | Boots to capture **per phase** (default + debug). Default `3` → 6 total boots. Values `< 1` fall back to `3`. |
| `--compare-builds` | No | Number of previously recorded builds to compare this run against for `previous_build_regressions` (e.g. `3` compares against the 3 immediately preceding builds). Defaults to **all** previously recorded builds; if fewer than N exist, compares against however many do. |
| `--resume-pull` | No | Skip login and the boot loop entirely — adb is already enabled (serial) or the device is already reachable (SSH), and all boots' logs are already on the device from a prior run that failed partway through the pull. Just pulls logs and (re)builds the report. Requires `--target` and `--build-path`. |
| `--host` | No | SSH hostname/IP of the device. **Presence selects SSH mode** instead of serial console — power-cycling between boots becomes a soft `reboot` over SSH (no TAC), and logs are pulled via SFTP (no adb). Mutually exclusive with `--com-port`/`--tac-port`. Requires `paramiko` (`pip install paramiko`). |
| `--user` | No | SSH username. Default `root`. |
| `--password` | No | SSH password. If omitted and `--key` isn't given, prompted interactively when `--host` is used. |
| `--key` | No | Path to an SSH private key file, used instead of a password. |
| `--ssh-port` | No | SSH port. Default `22`. |

### Build labeling

There is no nightly-build-share lookup. If `--build-path` isn't given,
`capture.py` scans this target's existing `bootchart-data-<slug>.json`
for the highest `Build-<N>` label already recorded and uses `Build-<N+1>`
(starting at `Build-1` if none exist yet). This label is what's recorded
in the JSON and shown in the HTML report's "Build" row — it is not a
build artifact path of any kind.

### Workflow

1. Connect to the device: if `--host` is given, open an SSH connection;
   otherwise open the serial console (auto-detecting the port if not
   given) and log in.
2. If `--target` wasn't given, detect it via `uname -n`.
3. If `--build-path` wasn't given, compute the next `Build-<N>` label for
   this target.
4. Collect device info (machine/SoC/OS/kernel) over the active
   connection.
5. Boot loop: `--num-boots` default-cmdline boots, power-cycling between
   boots (Alpaca TAC in serial mode, a soft `reboot` over SSH polled
   until the device comes back in SSH mode), collecting logs on-device
   after each; then, before the first debug boot, inject debug kernel
   cmdline params (`initcall_debug`, `systemd.log_level=debug`,
   `log_buf_len=4M`) into the device's current bootloader entry; then
   `--num-boots` more boots under that debug cmdline, collecting logs
   the same way.
6. Serial mode: enable `adb` on the device, then pull all collected logs
   via `adb pull`. SSH mode: pull all collected logs via SFTP (no adb
   involved).
7. Parse every boot's logs into structured metrics
   (`capture.build_run`); only the first boot of each phase (e.g.
   `default-1`, `debug-1`) is recorded into the JSON/HTML report, but
   every boot is pulled, parsed, and permanently backed up as a `.txt`
   file under `report/Boot-Logs/`.
8. Run `regression_analysis.build_analysis(...)` over all captured boots
   and attach the resulting structured findings — plus the two "Not yet
   analyzed" placeholder strings — to each displayed boot. **This step
   always runs** (it's cheap, pure-function, no I/O); what's gated behind
   explicit user request is turning those structured findings into
   written analysis (see "Analysis is opt-in" above).
9. Hand off to `report.py` (`add_run` → `save_data` → `render`) to update
   the JSON history and regenerate the HTML.

### Outputs

- `report/Boot-Charts/bootchart-data-<slug>.json` — updated with the new
  run.
- `report/Boot-Charts/bootchart-overview-<slug>.html` — regenerated from
  that JSON.
- `report/Boot-Logs/<target>/<build-folder>/Logs-<log_name>/*.txt` — raw
  pulled logs for every boot captured (not just the displayed ones).
- `report/Boot-Logs/<target>/<build-folder>/<timestamp>.txt` — permanent
  per-displayed-boot backup (JSON only keeps the most recent 30 runs;
  these backups don't expire).

### Error handling

- No login/shell prompt within `--boot-timeout` (serial mode) → raises
  with the partial console buffer; check the device is powered on and
  the console port is correct.
- SSH connect failure (`--host`) → raises with the underlying paramiko
  error; check the host/user/credentials and that the device is
  reachable on `--ssh-port`.
- SSH soft reboot doesn't come back within `--boot-timeout` → raises;
  the device may have failed to reboot cleanly or isn't coming back up
  on the network — check it manually (there's no TAC to force a power
  cycle in SSH mode).
- `--host` given together with `--com-port`/`--tac-port` → exits
  immediately with an error; these transports are mutually exclusive.
- `bootctl` reports Boot Loader Specification Type #2 (or unrecognized)
  → debug cmdline injection is skipped with a printed notice; capture
  continues (debug boots simply won't carry kernel-probe timing).
  `capture.py` detects this from `metrics.kernel.hitters` being absent at
  render time, not from a threaded flag.
- A failed pull partway through (`adb pull` or SFTP) → rerun with
  `--resume-pull --target <target> --build-path <same label>` (add
  `--host`/`--user`/etc. if it was an SSH-mode capture) once the
  device-side issue is fixed, instead of repeating the whole boot loop.

---

## Report stage

**Use when:** no device is involved — just regenerate the HTML from
existing JSON, manually append a run JSON, or attach agent-authored
analysis text to a boot.

**Entry point:** `py -3 report/report.py --target <target> [options]`

`--target` is always required (used to resolve
`bootchart-data-<slug>.json` / `bootchart-overview-<slug>.html`).

### `--report-cmd` modes

| Mode | Description |
|---|---|
| `render` (default) | Just regenerates the HTML from the existing JSON. No other flags needed besides `--target`. |
| `add-run` | Appends a run from `--run-json <path\|->` (a path, or `-` for stdin) to the JSON, then renders. |
| `set-analysis` | Writes agent-authored `overall_overheads_text` / `optimization_possibilities` onto one boot (named by `--log-name`) in the **most recently recorded run**, then renders. Requires `--log-name` and at least one of `--overheads-file` / `--optimization-file`. This is the only supported way to fill in analysis prose — never hand-edit the JSON directly. |

### Inputs

| Flag | Used with | Description |
|---|---|---|
| `--target` | always | Target name, required for every mode. |
| `--run-json` | `add-run` | Path to a run JSON, or `-` for stdin. |
| `--log-name` | `set-analysis` | `log_name` of the boot to annotate (e.g. `default-1`, `debug-1`) within the most recent run. |
| `--overheads-file` | `set-analysis` | Path to a text file whose contents become the boot's `overall_overheads_text`. |
| `--optimization-file` | `set-analysis` | Path to a text file whose contents become the boot's `optimization_possibilities`. |

### The JSON/HTML contract

`bootchart-data-<slug>.json` is the only file ever hand-edited (by
appending a run or setting analysis text). `bootchart-overview-<slug>.html`
is **always** fully derived from that JSON by `report.py` — never
hand-authored, never edited directly. Re-running `render` regenerates it
byte-for-byte from whatever is currently in the JSON.

### Outputs

- `report/Boot-Charts/bootchart-data-<slug>.json`
- `report/Boot-Charts/bootchart-overview-<slug>.html`
- A `.bak.txt`-style backup is not produced by `report.py` itself — permanent
  per-boot backups are written by `capture.py` (`save_run_backup`) at
  capture time, not by `report.py`.

### Error handling

- `--report-cmd add-run` without `--run-json`, or `set-analysis` without
  `--log-name`/an `-file` flag → prints the missing requirement and exits
  non-zero before touching the JSON.
- `set-analysis` with no runs recorded yet, or a `--log-name` not present
  in the most recent run → prints the available `log_name`s (or "no runs
  yet") and exits non-zero.

---

## What the report shows

- **Device Information** — Machine / SoC / OS / Kernel / Serial /
  Captured, one column per captured build. Serial is `adb devices`' own
  device id, not a value read off the device over serial console.
- **Build** row — the `Build-<N>` label (or legacy path) this run was
  captured against.
- **Boot-time metrics table** — NHLOS, kernel, initramfs, sysinit_svc,
  total_sysinit, total_multiuser, each with its own note and (where
  measurable) ranked high-hitters inline.
- **Overall Suspected Overheads** / **Optimization Possibilities** rows —
  agent-authored prose, populated only once analysis has actually been
  run and committed via `--report-cmd set-analysis`. Shows the "Not yet
  analyzed" placeholder otherwise.

## Prerequisites

```
py -3 -m pip install pyserial comtypes
```

or

```
python3 -m pip install pyserial comtypes
```

- `pyserial` — serial console login, command execution, port
  auto-detection (capture stage, serial mode only).
- `comtypes` — Alpaca TAC COM automation, used only to power-cycle the
  device between boots in serial mode (capture stage only).
- `adb` must be on `PATH` — used by capture in serial mode to enable adb
  on-device and pull logs.
- `paramiko` (`pip install paramiko`) — only needed for SSH mode
  (`--host`): login, command execution, soft reboot, and SFTP log pull.
  Not required for serial-mode capture.
- No flashing tool (PCAT, EDL, fastboot, etc.) is required or used by this
  skill.

Run with this machine's ARM64 Python launcher (`py -3 ...`), or a regular
Windows Python install (`python3 ...`).
