# Qualcomm Linux Bootbench Skill

AI-powered boot-time flash / capture / report pipeline for Qualcomm Linux SoCs.
Designed to be used through natural language prompts to an AI agent (Cline, Cursor, etc.).
Runs entirely from the **host machine** against a physically-connected target device.

**Supported devices:** Qualcomm Linux (QLI) target devices (e.g. `iq-9075-evk`)

**Supported transports:** Serial console (COM) + Alpaca TAC (power/EDL) + PCAT (EDL flash) + ADB (log pull)

**Supported host platforms:** Windows (ARM64 or x64)

---

## Quick Start (2 Minutes)

### Prerequisites
```bash
py -3 -m pip install pyserial comtypes
```
- `pyserial` — serial console login / command execution / COM-port auto-detection (used by `flash` + `capture`)
- `comtypes` — Alpaca TAC COM automation for power-cycle / EDL entry (used by `flash` only)
- `adb` must be on `PATH` — used by `capture` to pull logs off the device
- Qualcomm **PCAT** installed at `C:\Program Files (x86)\Qualcomm\PCAT\bin\PCAT.exe` (used by `flash`)
- Network access to the nightly build share (`\\swayam\QLI_Builds\Yocto`)

### Your First Run
```bash
# Step 1: Physically connect the device (USB serial + Alpaca TAC + adb cable)

# Step 2: Tell the agent
"Flash the device with the latest nightly build and give me the boot report"

# Step 3: View report
# Open report/Boot-Charts/bootchart-overview-<target>.html
```

### Common Tasks

| Task | Prompt |
|------|--------|
| Flash only (latest nightly) | `"Flash the device with the latest nightly build"` |
| Flash a specific target/memory type | `"Flash iq-9075-evk with UFS memory, skip the confirmation"` |
| Full pipeline in one command | `"Flash and give me the boot-time report"` |
| Capture only (device already flashed) | `"Device is already flashed, just give me fresh boot numbers"` |
| Regenerate the HTML report | `"Regenerate the boot report from the existing JSON"` |
| Append a run JSON manually | `"Append this run JSON to the boot report"` |
| Write agent analysis onto a boot | `"Write the overheads/optimization analysis onto default-1"` |
| Recover a stuck device | `"The device is stuck in EDL mode, recover it"` |

See [Stages](#stages) below for detailed instructions.

---

## Repository Structure

```
bootbench/                        ← this skill's root (installed at .claude/skills/bootbench/)
├── SKILL.md                        (agent-discoverable; routes between the three stages)
├── README.md                       (this file)
├── flash/                          ← Stage: flash a device over EDL via PCAT
│   ├── SKILL.md
│   └── flash.py
├── capture/                        ← Stage: boot an already-flashed device, collect boot-time data
│   ├── SKILL.md
│   ├── capture.py
│   ├── regression_analysis.py        ← co-located, imported directly
│   └── reference/                    ← COT docs guiding agent-authored analysis text
│       ├── COT_outlier_detection.md
│       ├── COT_regression_rca.md
│       └── COT_optimization_recommendations.md
└── report/                         ← Stage: regenerate/maintain the HTML report, no device involved
    ├── SKILL.md
    ├── report.py
    └── Boot-Charts/, Boot-Logs/      (generated output, created once a capture/report has run)
```

Only `bootbench/SKILL.md` is agent-discoverable — it's what gets listed
and invoked as the `bootbench` skill. The per-stage `SKILL.md` files
(`flash/SKILL.md`, `capture/SKILL.md`, `report/SKILL.md`) are reference
documentation for whichever stage is actually run; the root `SKILL.md`
routes between them.

---

## Stages

**Skill:** `SKILL.md`

Three independent stages — the agent picks the right one from what you ask for.

### Stage 1: flash
Flash a Qualcomm target device with a nightly Yocto build over EDL via PCAT.
Resolves the build, detects the target/memory type, enters EDL via Alpaca TAC, and flashes.

```
Nightly share → Find latest build → Detect target + memory → EDL → PCAT flash → Device flashed
```

**Example prompts:**
```
"Flash the device with the latest nightly build"
"Flash iq-9075-evk with UFS memory, skip the confirmation prompt"
"Dry-run a flash so I can see what build/target it would use"
"The device is stuck in EDL/Sahara mode, recover it"
```

Full stage docs: `flash/SKILL.md`

---

### Stage 2: capture
Boot an already-flashed device N times (default: 3) under the default kernel
cmdline, then N more with debug cmdline params, pull the logs via adb, parse
them into metrics, run outlier / regression / root-cause analysis, and hand
the result to `report.py` to record and render — **without reflashing.**

```
Device (flashed) → Boot N × default + N × debug → adb pull → Parse → regression_analysis → report.py
```

**Example prompts:**
```
"Device is already flashed, just give me fresh boot numbers"
"Run 5 boots per phase and record them in the report"
"Capture boot-time data from the connected device"
```

Each capture produces 6 boots (3 default-cmdline + 3 debug-cmdline). Only
one boot per phase is *displayed* in the report, but all 6 feed the
outlier / intra-run-regression / previous-build-regression / high-hitter
analysis.

`capture.py` populates each displayed boot's `regression_analysis` dict
with raw findings plus two `"Not yet analyzed"` placeholders
(`overall_overheads_text`, `optimization_possibilities`). Turning those
placeholders into real prose is an **agent task** — see
`capture/reference/COT_outlier_detection.md`,
`COT_regression_rca.md`, and `COT_optimization_recommendations.md`,
then commit the prose with `report.py --report-cmd set-analysis` (see
stage 3).

Full stage docs: `capture/SKILL.md`

---

### Stage 3: report
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

Full stage docs: `report/SKILL.md`

---

### Full pipeline in one command

If the user explicitly wants the whole flash → capture → report pipeline
in one go, `flash.py --all` chains straight into capture + report
in-process, reusing the target / build-path / COM port it just resolved.

```bash
py -3 flash/flash.py --all --yes
py -3 flash/flash.py --all --num-boots 5 --yes
```

This is the exception, not the default. Prefer invoking just the stage
the user actually asked for.

---

## Tell the agent

Connect your device (USB serial + Alpaca TAC + adb cable, as applicable
to the stage) and describe the task in plain English. The agent reads
`SKILL.md` and routes to the right stage:

- Needs to reflash → **flash** (standalone)
- Already flashed, want fresh boot numbers → **capture** (standalone)
- No device, just regenerate / append to the report → **report** (standalone)
- Whole pipeline in one command → **flash** with `--all`

### What the AI agent will ask you

If information is missing, the agent will ask:
- **"Which target?"** (if `uname -n` can't be auto-detected over serial)
- **"Which COM port?"** (if no live serial console is found on any port)
- **"Which Alpaca TAC port?"** (if multiple TAC devices are visible)
- **"UFS or EMMC?"** (if `lsblk` can't be auto-classified)
- **"Confirm the flash plan?"** (shown right before PCAT runs, unless `--yes`)
- **"Which boot to annotate?"** (`--log-name` for `set-analysis`, e.g. `default-1` / `debug-1`)

---

## What the report shows

The HTML report (`bootchart-overview-<target>.html`) is always fully
derived from `bootchart-data-<target>.json` — never hand-authored. Each
recorded run contributes two displayed boots (`default-1`, `debug-1`),
and each displayed boot carries:

### Device & Build Information
```
Target:     iq-9075-evk
Memory:     UFS
Build:      ..._Nightly_Build_master_<N>
Captured:   <timestamp>
```

### Boot-time Metrics Table
Per-boot timing from `systemd-analyze blame` / `critical-chain`,
kernel-probe hitters (debug boots only), and `systemctl` failed units —
all parsed into structured JSON and rendered as a flat table.

### Regression / Outlier Findings
`regression_analysis` dict attached to each displayed boot, with:
- **`outliers`** — per-phase findings, `kind: "duration"` (MAD-based vs. sibling boots) or `kind: "status_divergence"` (failed-in-some-not-others)
- **`intra_run_regressions`** — named units that regressed >15% vs. their sibling-boot baseline
- **`previous_build_regressions`** — regressions vs. the equivalent phase/boot in the target's previous recorded build
- **`high_hitters`** — biggest-duration named units per displayed boot, plus always-failing units/probes
- **`unit_appeared_once`** — units present in only 1 of 3 sibling boots (manual-investigation hint)

### Agent-Authored Analysis (per boot)
- **`overall_overheads_text`** — outliers → intra-run regressions → previous-build regressions → high hitters → root-cause analysis (five subsections, always in that order; see `capture/reference/COT_outlier_detection.md` + `COT_regression_rca.md`)
- **`optimization_possibilities`** — one recommended approach per RCA'd finding (see `capture/reference/COT_optimization_recommendations.md`)

Both render through the same plain bullet-per-line HTML helper (one
`<li>` per newline, or a single `<div>` for one-paragraph text) — no
tables, no charts, no nested structures. Keep the dashboard simple.

---

## Stage Pipeline (underlying scripts)

| Stage / Component | Purpose | Script |
|---|---|---|
| **flash** | Flash target via EDL + PCAT, auto-detect target & memory type | `flash/flash.py` |
| **capture** | Boot N×default + N×debug, pull logs, parse, record | `capture/capture.py` |
| **regression_analysis** | Outlier / intra-run / previous-build / high-hitter analysis (co-located with capture) | `capture/regression_analysis.py` |
| **report** | Maintain JSON history, render HTML, commit agent analysis text | `report/report.py` |

### Cross-module imports

`capture.py` has no serial-console or report-rendering logic of its own —
it does a plain `import flash` and `import report` and delegates to
them. `flash.py --all` similarly does `import capture` / `import report`,
but only when `--all` is actually passed, so a bare `flash.py`
invocation (flash-only deployment) never needs `capture.py`/`report.py`
present at all.

Each script has an `_ensure_importable(module_name)` helper that checks
both `<its own folder>/<module_name>.py` and
`<parent of its own folder>/<module_name>/<module_name>.py`, so moving
these scripts between a flat layout and a per-usecase layout never
breaks the required imports.

`report.py` has no cross-module imports at all.

---

## Data Collection

Files pulled from device per boot (by `capture.py` over adb):

| File | Source | Used for |
|---|---|---|
| `blame.txt` | `systemd-analyze blame` | Per-unit activation durations |
| `critical_chain.txt` | `systemd-analyze critical-chain` | Boot-critical dependency chain |
| `blame_systemdsystemctl.txt` | `systemctl --failed` + friends | Failed-unit extraction (`parse_failed_units`) |
| `dmesg.txt` | `dmesg` | Kernel-probe hitters, RCA evidence |
| `journalctl.log` | `journalctl -b` | Userspace unit RCA evidence |
| `cmdline.txt` | `/proc/cmdline` | Phase tagging (default vs. debug) |

---

## Requirements & Compatibility

### Host Machine

| Requirement | Notes |
|---|---|
| **Python 3** | Run with ARM64 `py -3` or regular Windows `python3` |
| **pyserial** | `pip install pyserial` — serial console (flash + capture) |
| **comtypes** | `pip install comtypes` — Alpaca TAC COM automation (flash only) |
| **adb** | Required on `PATH` — used by `capture` to pull logs |
| **PCAT** | `C:\Program Files (x86)\Qualcomm\PCAT\bin\PCAT.exe` — used by `flash` |
| **OS** | Windows only (comtypes + PCAT) |

### Device

| Requirement | Notes |
|---|---|
| **Platform** | Qualcomm Linux target device (e.g. `iq-9075-evk`) |
| **Memory** | UFS or EMMC (auto-detected via `lsblk`) |
| **Connections** | Serial (COM) console, Alpaca TAC debug board, adb cable |

### Qualcomm-Specific Features

- EDL / Sahara flash via PCAT (Qualcomm-only)
- Alpaca TAC `BootToEDL` and power-cycle automation (Qualcomm-only)
- Nightly build layout at `\\swayam\QLI_Builds\Yocto\..._Nightly_Build_master_<N>\performance\qcom-multimedia-proprietary-image-<target>\` (Qualcomm-only)

---

## Quick Commands

```bash
# Flash only — waits for a manual y/N confirmation right before flashing
py -3 flash/flash.py

# Flash with explicit target/memory-type, skip confirmation
py -3 flash/flash.py --target iq-9075-evk --memory-type UFS --yes

# Resolve the plan without touching the device
py -3 flash/flash.py --dry-run

# Flash, then capture + report in one command
py -3 flash/flash.py --all --yes

# Recover a device stuck in EDL/Sahara mode
py -3 flash/flash.py --recover

# Capture only (device already flashed)
py -3 capture/capture.py --build-path "\\swayam\...\performance"

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
| `SKILL.md` | Agent-discoverable skill root; routes between flash / capture / report |
| `flash/SKILL.md` | Flash stage — inputs, workflow, outputs, error handling |
| `capture/SKILL.md` | Capture stage — two-phase boot loop, regression analysis, placeholder-to-prose workflow |
| `capture/reference/COT_outlier_detection.md` | COT guiding `overall_overheads_text` prose (five subsections, always in order) |
| `capture/reference/COT_regression_rca.md` | COT guiding the RCA subsection — evidence-driven, no fixes |
| `capture/reference/COT_optimization_recommendations.md` | COT guiding `optimization_possibilities` prose — fix/strategy only |
| `report/SKILL.md` | Report stage — `--report-cmd` values (`render` / `add-run` / `set-analysis`) and rendering contract |
