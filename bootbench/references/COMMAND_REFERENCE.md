# Command Reference

## Invocation

```
bootbench.py {flash,capture,report,all} [{flash,capture,report,all} ...] [options]
```

Stages can be listed in any order on the command line, but always execute
in `flash` → `capture` → `report` order. `capture` records and renders its
own report at the end, so a separate `report` invocation is only needed
when `capture` isn't part of the same command.

## Common flags (apply to `flash` and/or `capture`)

- `--target TARGET` — override/declare target name (e.g. `iq-9075-evk`).
  Auto-detected via serial otherwise, except `report` alone or `capture
  --resume-pull`, which require it explicitly.
- `--com-port COM_PORT` — serial console COM port (auto-detected if omitted)
- `--boot-timeout SECONDS` — login-prompt wait timeout (default: 480)

## `flash` flags

- `--tac-port TAC_PORT` — Alpaca TAC COM port (auto-detected if only one TAC device)
- `--dry-run` — resolve paths and print the plan, then exit before touching the device
- `--yes` — skip the y/N confirmation prompt before flashing
- `--skip-edl` — device is already in EDL mode; skip the TAC BootToEDL step
- `--recover` — power-cycle the device via TAC out of EDL/Sahara, then exit (requires the `flash` stage)

## `capture` flags

- `--build-path PATH` — nightly build path used for this boot (required if `capture` runs without `flash` in the same command; supplied automatically otherwise)
- `--num-boots N` — number of consecutive boots to capture **per phase**
  (default: 3 → 3 default-cmdline + 3 debug-cmdline = 6 boots total).
  **Values below 1 fall back to 3, with a printed warning** — this is
  validated once, in `main()`, before any device interaction. All N boots
  per phase are still captured and permanently backed up under
  `Boot-Logs/`; only the first boot of each phase is recorded into the
  JSON/HTML report.
- `--resume-pull` — skip login/boot-loop entirely: adb is already enabled
  and all boots' `/data/Logs-<n>` dirs are already on the device (a prior
  run reached `adb pull` and failed partway) — just pull logs and
  (re)build the report. Requires `--target` and `--build-path`. Does
  **not** call the debug-cmdline-editing step, so whether that boot's
  debug prints were actually active is inferred from its pulled `dmesg`
  content, not assumed.

## `report` flags

- `--report-cmd {render,add-run}` — `render` (default) just regenerates
  the HTML from the existing JSON; `add-run` appends `--run-json` first
- `--run-json PATH` — run JSON to append (or `-` for stdin), used with `--report-cmd add-run`

## Worked examples

```
# Flash only -- waits for a manual y/N confirmation right before flashing
py -3 bootbench.py flash

# Flash, then capture boot-time logs (skip the y/N prompt with --yes)
py -3 bootbench.py flash capture --yes

# Flash, capture, and report -- the full pipeline in one command
py -3 bootbench.py all --yes

# Capture only, on a device that's already flashed with this build
py -3 bootbench.py capture --build-path "\\swayam\...\performance"

# Capture with more boots per phase (still only 1 default + 1 debug shown in the report)
py -3 bootbench.py capture --build-path "\\swayam\...\performance" --num-boots 5

# Resume a capture that flashed/booted fine but failed during the adb-pull/report step
py -3 bootbench.py capture --build-path "\\swayam\...\performance" --target iq-9075-evk --resume-pull

# Recover a device stuck in EDL/Sahara mode
py -3 bootbench.py flash --recover

# Re-render the HTML report from the existing JSON (no device involved)
py -3 bootbench.py report --report-cmd render --target iq-9075-evk

# Manually append a run JSON to the report (no device involved)
py -3 bootbench.py report --report-cmd add-run --run-json new_run.json --target iq-9075-evk
```

## Recovery / resume options

- `flash --recover`: device stuck in EDL/Sahara after an aborted flash →
  power-cycles it back to normal boot and exits.
- `capture --resume-pull` (requires `--target` and `--build-path`):
  flashing and all boots already succeeded but the `adb pull`/report step
  failed partway → re-runs just the pull + report step without repeating
  the flash or any boots.
- **Ctrl+C during a flash**: kills the PCAT process and automatically
  power-cycles the device back to normal boot.

## Prerequisites

```
py -3 -m pip install pyserial comtypes
```

- `pyserial` — required for both `flash` and `capture` (serial console
  login, command execution, port auto-detection)
- `comtypes` — required for `flash` only (Alpaca TAC COM automation for
  power-cycle / EDL entry)
- `adb` must also be on `PATH` — used by `capture` to pull logs off the
  device after boot capture

Run with either the ARM64 Python launcher (`py -3 bootbench.py ...`) or a
regular Windows Python install (`python3 bootbench.py ...`).
