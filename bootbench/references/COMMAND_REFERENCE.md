# bootbench.py -- command reference

```
bootbench.py {flash,capture,report,all} [{flash,capture,report,all} ...] [options]
```

Stages are positional and combinable in one invocation, in any order -- they always execute
in pipeline order (`flash` -> `capture` -> `report`) regardless of how they were typed.
`capture` already records and renders its own JSON/HTML report as part of pulling logs, so a
standalone `report` stage only does anything when `capture` is *not* also requested in the
same command.

## Common flags (apply to `flash` and/or `capture`)

- `--target TARGET` -- override/declare target name (e.g. `iq-9075-evk`). Required for
  `report` or `capture --resume-pull` run standalone; auto-detected via serial console
  (`uname -n`) by `flash` or plain `capture` otherwise.
- `--com-port COM_PORT` -- serial console COM port used for login/detection (auto-detected by
  probing every enumerated port if omitted).
- `--boot-timeout SECONDS` -- seconds to wait for a login prompt, on first login and after
  each reboot (default: 480).

## `flash` flags

- `--tac-port TAC_PORT` -- Alpaca TAC COM port name, e.g. `VTP8` (auto-detected if only one
  TAC device is connected).
- `--dry-run` -- resolve the build path and target, print the plan, then exit before touching
  the device (stops before EDL mode).
- `--yes` -- skip the y/N confirmation prompt right before flashing (otherwise waits for a
  manual answer after entering EDL mode).
- `--skip-edl` -- device is already in EDL mode; skip the TAC `BootToEDL` step.
- `--recover` -- power-cycle the device via TAC (out of EDL/Sahara back to normal boot) and
  exit. Must be combined with the `flash` stage.

## `capture` flags

- `--build-path PATH` -- nightly build path this boot used, e.g.
  `\\swayam\...\performance`. Required if `capture` runs without `flash` in the same command
  (supplied automatically when `flash` ran first).
- `--num-boots N` -- number of consecutive boots to capture (default: 3).
- `--resume-pull` -- skip login and the boot loop entirely: adb is already enabled and every
  boot's `/data/Logs-<n>` is already on the device (a prior run reached `adb pull` and failed
  partway). Just pulls logs and (re)builds the report. Requires `--target` and `--build-path`.

## `report` flags

- `--report-cmd {render,add-run}` -- `render` (default) just regenerates the HTML from the
  existing JSON; `add-run` appends `--run-json` first, then renders.
- `--run-json PATH` -- path to a run JSON to append (or `-` for stdin), used with
  `--report-cmd add-run`.

## Worked examples

```
# Flash only -- waits for a manual y/N confirmation right before flashing
py -3 scripts/bootbench.py flash

# Flash, then capture boot-time logs (skip the y/N prompt with --yes)
py -3 scripts/bootbench.py flash capture --yes

# Flash, capture, and report -- the full pipeline in one command
py -3 scripts/bootbench.py all --yes
py -3 scripts/bootbench.py flash capture report --yes

# Capture only, on a device that's already flashed with this build
py -3 scripts/bootbench.py capture --build-path "\\swayam\...\performance"

# Resume a capture that flashed/booted fine but failed during the adb-pull/report step
py -3 scripts/bootbench.py capture --build-path "\\swayam\...\performance" ^
    --target iq-9075-evk --resume-pull

# Recover a device stuck in EDL/Sahara mode
py -3 scripts/bootbench.py flash --recover

# Re-render the HTML report from the existing JSON (no device involved)
py -3 scripts/bootbench.py report --report-cmd render --target iq-9075-evk

# Manually append a run JSON to the report (no device involved)
py -3 scripts/bootbench.py report --report-cmd add-run --run-json new_run.json ^
    --target iq-9075-evk
```

## Recovery / resume options

- `flash --recover`: device stuck in EDL/Sahara after an aborted flash -> power-cycles it back
  to normal boot and exits.
- `capture --resume-pull` (requires `--target` and `--build-path`): flashing and all boots
  already succeeded but the `adb pull`/report step failed partway -> re-runs just the pull +
  report step without repeating the flash or any boots.
- **Ctrl+C during a flash**: kills the PCAT process and automatically power-cycles the device
  back to normal boot.

## Prerequisites

```
py -3 -m pip install pyserial comtypes
```

- `pyserial` -- required for both `flash` and `capture` (serial console login, command
  execution, port auto-detection).
- `comtypes` -- required for `flash` only (Alpaca TAC COM automation for power-cycle/EDL
  entry).
- `adb` must be on `PATH` -- used by `capture` to pull logs off the device after boot capture.

Run with the ARM64 Python launcher on this machine (`py -3`), or a regular Windows Python
install (`python3`).
