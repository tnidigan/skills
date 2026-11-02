# Qualcomm Linux Bootbench Skill

AI-powered boot-time capture / report pipeline for Qualcomm Linux SoCs.
Designed to be used through natural language prompts to an AI agent (Cline, Cursor, etc.).
Runs entirely from the **host machine** against a physically-connected target device.

This skill does **not** flash devices. It assumes the target already has
the build under test on it, and only boots/collects/analyzes boot-time
data from it.

**Supported devices:** Qualcomm Linux (QLI) target devices (e.g. `iq-9075-evk`)

**Supported transports:**
- Serial console (COM) + Alpaca TAC (power-cycle) + ADB (log pull) — default
- SSH (`--host`) + soft reboot (power-cycle) + SFTP (log pull) — no TAC/adb needed

**Supported host platforms:** Windows (ARM64 or x64)

---

## Quick Start (2 Minutes)

### Prerequisites
```bash
py -3 -m pip install pyserial comtypes
```
- `pyserial` — serial console login / command execution / COM-port auto-detection (serial mode)
- `comtypes` — Alpaca TAC COM automation, used only to power-cycle the device between boots (serial mode)
- `adb` must be on `PATH` — used to enable adb on-device and pull logs off it (serial mode)
- `paramiko` (`pip install paramiko`) — only needed for SSH mode (`--host`); not required for serial-mode capture

No flashing tool (PCAT, EDL, fastboot, etc.) and no network build share are
required by this skill.

### Your First Run
```bash
# Step 1: Physically connect the already-flashed device (USB serial + Alpaca TAC + adb cable)

# Step 2: Tell the agent
"Capture boot logs from the device and give me a summary"

# Step 3: View report
# Open report/Boot-Charts/bootchart-overview-<target>.html
```

### Common Tasks

| Task | Prompt |
|------|--------|
| Capture only, no analysis | `"Capture boot logs and give me a summary"` |
| Capture + full analysis | `"Capture boot logs, analyse them, and give me a summary"` |
| Capture with a custom build label | `"Capture boot logs and record them under Build-7"` |
| More boots per phase | `"Run 5 boots per phase and record them in the report"` |
| Regenerate the HTML report | `"Regenerate the boot report from the existing JSON"` |
| Append a run JSON manually | `"Append this run JSON to the boot report"` |
| Write agent analysis onto a boot | `"Write the overheads/optimization analysis onto default-1"` |
| Resume a failed pull | `"The last capture failed during adb pull, resume it"` |
| Compare against the last N builds only | `"Capture boot logs and compare regressions against the last 3 builds"` |

See [Stages](#stages) below for detailed instructions.

---

## Analysis is opt-in

Capture and analysis (outlier detection, regression/root-cause analysis,
optimization recommendations) are separate steps. **Capturing boot logs
never triggers analysis automatically.**

- "Capture boot logs and give me a summary" → capture only. The agent
  reports the device info (Machine, SoC, OS, Kernel, Serial, Captured
  time), the build label, and the raw boot-time metrics; the report's
  overheads/optimization rows stay at their "Not yet analyzed" placeholder.
- "Capture boot logs, analyse them, and give me a summary" → capture,
  then the agent works through the outlier/regression findings and
  writes the overheads + optimization prose into the report.

If a request is ambiguous about whether analysis is wanted, the agent
defaults to capture-only — analysis can always be added afterward via
`report.py --report-cmd set-analysis` without re-capturing. See
`SKILL.md` for the full routing rule and analysis workflow.

---

## Repository Structure

```
bootbench/                        ← this skill's root (installed at .claude/skills/bootbench/)
├── SKILL.md                        (agent-discoverable; the ONLY SKILL.md in this tree)
├── README.md                       (this file)
├── capture/                        ← Stage: boot an already-flashed device, collect boot-time data
│   ├── scripts/
│   │   ├── capture.py                 ← entry point; serial console + adb + Build-N labeling
│   │   └── regression_analysis.py     ← outlier/regression/high-hitter detection (pure functions)
│   └── reference/                    ← COT docs guiding agent-authored analysis text
│       ├── COT_outlier_detection.md
│       ├── COT_regression_rca.md
│       └── COT_optimization_recommendations.md
└── report/                         ← Stage: regenerate/maintain the HTML report, no device involved
    ├── report.py
    └── Boot-Charts/, Boot-Logs/      (generated output, created once capture/report has run)
```

Only `bootbench/SKILL.md` is agent-discoverable — it's what gets listed
and invoked as the `bootbench` skill, and it contains the full
documentation for both stages. This README is a human-facing overview;
`SKILL.md` is the source of truth for inputs/workflow/outputs.

---

## Stages

**Skill:** `SKILL.md`

Two stages — the agent picks the right one (or both) from what you ask for.

### Stage 1: capture
Boot an already-flashed device N times (default: 3) under the default kernel
cmdline, then N more with debug cmdline params, pull the logs via adb, parse
them into metrics, and hand the result to `report.py` to record and render —
**no flashing involved.**

```
Device (already flashed) → Boot N × default + N × debug → adb pull → Parse → report.py
```

**Example prompts:**
```
"Capture boot logs and give me a summary"
"Capture boot logs, analyse them, and give me a summary"
"Run 5 boots per phase and record them in the report"
```

Each capture produces 6 boots (3 default-cmdline + 3 debug-cmdline). Only
one boot per phase is *displayed* in the report, but all 6 feed the
outlier / intra-run-regression / previous-build-regression / high-hitter
analysis, which is always computed and attached to the run — whether or
not it's turned into written analysis depends on what was asked (see
"Analysis is opt-in" above).

Each capture that doesn't get an explicit `--build-path` is labeled with
an auto-incrementing `Build-<N>` tag (`Build-1`, `Build-2`, ...) per
target — there is no nightly-build-share lookup.

`capture.py` populates each displayed boot's `regression_analysis` dict
with raw findings plus two `"Not yet analyzed"` placeholders
(`overall_overheads_text`, `optimization_possibilities`). Turning those
placeholders into real prose — **only when the user explicitly asks for
analysis** — uses `capture/reference/COT_outlier_detection.md`,
`COT_regression_rca.md`, and `COT_optimization_recommendations.md`, then
commits the prose with `report.py --report-cmd set-analysis` (see stage 2).

Full stage docs: `SKILL.md` ("Capture stage" section)

---

### Stage 2: report
Maintains `bootchart-data-<slug>.json` (the only hand-edited file) and
regenerates `bootchart-overview-<slug>.html` from it. No device required.

```
Existing JSON history (+ optional run JSON / analysis text) → report.py → bootchart-overview-<slug>.html
```

**Example prompts:**
```
"Regenerate the boot report HTML from the existing JSON"
"Append this run JSON to the boot report"
"Write the overheads and optimization analysis onto default-1"
```

Three `--report-cmd` modes:
- **`render`** — regenerate the HTML from the existing JSON (no side effects beyond the `.html`)
- **`add-run`** — append a run from `--run-json`, then render
- **`set-analysis`** — write agent-authored analysis text (overheads +
  optimization) onto a single boot in the **most recently recorded run**,
  then render in the same step

Full stage docs: `SKILL.md` ("Report stage" section)

---

## Tell the agent

Connect your device (USB serial + Alpaca TAC + adb cable, or just network
reachability for SSH mode) and describe the task in plain English. The agent reads `SKILL.md` and routes between
capture and report, and decides whether analysis is in scope based on
your wording (see "Analysis is opt-in" above):

- Want fresh boot numbers (device already flashed) → **capture**
- Want numbers *and* RCA/outlier/optimization analysis → **capture, then analyze**
- No device, just regenerate / append to the report → **report**

### What the AI agent will ask you

If information is missing, the agent will ask:
- **"Which target?"** (if `uname -n` can't be auto-detected over the active connection)
- **"Serial console or SSH?"** (if it's not clear from context which transport to use)
- **"Which COM port?"** (serial mode, if no live serial console is found on any port)
- **"Which Alpaca TAC port?"** (serial mode, if multiple TAC devices are visible)
- **"Which host, and what user/credentials?"** (SSH mode, if `--host` is implied but connection info is missing)
- **"Which boot to annotate?"** (`--log-name` for `set-analysis`, e.g. `default-1` / `debug-1`)

---

## What the report shows

The HTML report (`bootchart-overview-<target>.html`) is always fully
derived from `bootchart-data-<target>.json` — never hand-authored. Each
recorded run contributes two displayed boots (`default-1`, `debug-1`),
and each displayed boot carries:

### Device & Build Information
```
Machine:    Qualcomm Technologies, Inc. Lemans EVK
SoC:        QCS9075 rev 2.0
OS:         Qualcomm Linux Reference Distro
Kernel:     7.2.0-...
Serial:     <adb device id>
Build:      Build-3
Captured:   <timestamp>
```

### Boot-time Metrics Table
Per-boot timing from `systemd-analyze blame` / `critical-chain`,
kernel-probe hitters (debug boots only), and `systemctl` failed units —
all parsed into structured JSON and rendered as a flat table.

### Regression / Outlier Findings
`regression_analysis` dict attached to each displayed boot (always
computed at capture time, regardless of whether analysis prose is
requested), with:
- **`outliers`** — per-phase findings, `kind: "duration"` (MAD-based vs. sibling boots) or `kind: "status_divergence"` (failed-in-some-not-others)
- **`intra_run_regressions`** — named units that regressed >15% vs. their sibling-boot baseline
- **`previous_build_regressions`** — regressions vs. the equivalent phase/boot in each previously recorded build being compared against (all of them by default, or the N most recent via `--compare-builds N`)
- **`high_hitters`** — biggest-duration named units per displayed boot, plus always-failing units/probes
- **`unit_appeared_once`** — units present in only 1 of 3 sibling boots (manual-investigation hint)

### Agent-Authored Analysis (per boot, written only on explicit request)
- **`overall_overheads_text`** — outliers → intra-run regressions → previous-build regressions → high hitters → root-cause analysis (five subsections, always in that order; see `capture/reference/COT_outlier_detection.md` + `COT_regression_rca.md`)
- **`optimization_possibilities`** — one recommended approach per RCA'd finding (see `capture/reference/COT_optimization_recommendations.md`)

Both render through the same plain bullet-per-line HTML helper (one
`<li>` per newline, or a single `<div>` for one-paragraph text) — no
tables, no charts, no nested structures. Keep the dashboard simple.

---

## Stage Pipeline (underlying scripts)

| Stage / Component | Purpose | Script |
|---|---|---|
| **capture** | Boot N×default + N×debug, pull logs, parse, record | `capture/scripts/capture.py` |
| **regression_analysis** | Outlier / intra-run / previous-build / high-hitter analysis (co-located with capture) | `capture/scripts/regression_analysis.py` |
| **report** | Maintain JSON history, render HTML, commit agent analysis text | `report/report.py` |

### Cross-module imports

`capture.py` has no report-rendering logic of its own — it imports
`report/report.py` directly (two directories up:
`.../capture/scripts/capture.py` → `.../report/report.py`) and delegates
to it for every JSON/HTML update. `regression_analysis.py` is co-located
in the same `capture/scripts/` folder, so no path shimming is needed to
import it.

`report.py` has no cross-module imports at all and runs fully standalone.

---

## Data Collection

Files pulled from device per boot (by `capture.py`, over adb in serial mode or SFTP in SSH mode):

| File | Source | Used for |
|---|---|---|
| `blame_systemd.txt` | `systemd-analyze blame` | Per-unit activation durations |
| `systemd-analyze_critical_chain.txt` / `..._sysinit.txt` | `systemd-analyze critical-chain` | Boot-critical dependency chain, per-stage hitters |
| `blame_systemdsystemctl.txt` | `systemctl` | Failed-unit extraction (`parse_failed_units`) |
| `dmesg.txt` | `dmesg` | Kernel-probe hitters (debug boots only), RCA evidence |
| `journalctl.log` | `journalctl -b` | Userspace milestone timing, RCA evidence |
| `systemd_analyze.txt` | `systemd-analyze` | Top-level firmware/loader/kernel/userspace timing |

---

## Requirements & Compatibility

### Host Machine

| Requirement | Notes |
|---|---|
| **Python 3** | Run with ARM64 `py -3` or regular Windows `python3` |
| **pyserial** | `pip install pyserial` — serial console login/commands (serial mode) |
| **comtypes** | `pip install comtypes` — Alpaca TAC power-cycle automation (serial mode) |
| **adb** | Required on `PATH` — used to enable adb on-device and pull logs (serial mode) |
| **paramiko** | `pip install paramiko` — SSH login/commands/reboot/SFTP (SSH mode, `--host`, only) |
| **OS** | Windows only (comtypes dependency applies to serial mode only; SSH mode has no OS-specific dependency) |

### Device

| Requirement | Notes |
|---|---|
| **Platform** | Qualcomm Linux target device (e.g. `iq-9075-evk`), already flashed with the build under test |
| **Connections (serial mode)** | Serial (COM) console, Alpaca TAC debug board, adb cable |
| **Connections (SSH mode)** | Network reachability to the device's SSH server — no serial, TAC, or adb cabling needed |

### Qualcomm-Specific Features

- Alpaca TAC power-cycle automation (Qualcomm-only), used only to force a
  clean reboot between boots
- `bootctl`-based debug kernel cmdline injection (Boot Loader
  Specification Type #1 devices only; skipped with a notice on Type #2)

---

## Quick Commands

```bash
# Capture only (device already flashed), auto-detecting target/port/build label
py -3 capture/scripts/capture.py

# Capture with explicit target/COM port and more boots per phase
py -3 capture/scripts/capture.py --target iq-9075-evk --com-port COM5 --num-boots 5

# Capture under an explicit build label instead of the auto-incrementing one
py -3 capture/scripts/capture.py --build-path "Build-7"

# Resume a capture that failed partway through adb pull
py -3 capture/scripts/capture.py --resume-pull --target iq-9075-evk --build-path Build-3

# Capture over SSH instead of serial console (no TAC/adb needed)
py -3 capture/scripts/capture.py --host 192.168.1.50 --user root --num-boots 5

# Compare this build's regressions against only its 3 most recent prior builds
# (default, with no --compare-builds, is to compare against ALL prior builds)
py -3 capture/scripts/capture.py --compare-builds 3

# Regenerate the HTML report from existing JSON
py -3 report/report.py --report-cmd render --target iq-9075-evk

# Append a run JSON
py -3 report/report.py --report-cmd add-run --run-json new_run.json --target iq-9075-evk

# Commit agent-authored analysis text onto a boot
py -3 report/report.py --report-cmd set-analysis --target iq-9075-evk --log-name default-1 \
    --overheads-file overheads.txt --optimization-file optimization.txt
```

---

## Documentation

| File | Description |
|---|---|
| `README.md` | This file — overview, quick-start, and compatibility |
| `SKILL.md` | Agent-discoverable skill root; full capture + report docs, analysis opt-in rule |
| `capture/reference/COT_outlier_detection.md` | COT guiding `overall_overheads_text` prose (five subsections, always in order) |
| `capture/reference/COT_regression_rca.md` | COT guiding the RCA subsection — evidence-driven, no fixes |
| `capture/reference/COT_optimization_recommendations.md` | COT guiding `optimization_possibilities` prose — fix/strategy only |
