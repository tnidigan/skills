---
name: bootbench
description: Flashes a Qualcomm EDL target device with the latest nightly Yocto build via PCAT, captures boot-time metrics (systemd-analyze, dmesg, journalctl, critical-chain) over N consecutive boots via serial console + adb, and records/renders a per-target JSON+HTML boot-time report. Use when asked to flash a device, capture boot logs, run bootbench, collect boot-time/boot-chart metrics, or regenerate a boot-time report.
compatibility: Windows only. Requires PCAT, an Alpaca TAC debug board, pyserial, comtypes, and adb on PATH.
---

`scripts/bootbench.py` is the single entry point for the flash -> boot -> capture -> report
pipeline. It exposes three composable stages, named as positional args and always executed
in this fixed order regardless of how they're typed: `flash` -> `capture` -> `report`.
`all` is shorthand for all three.

## Setup (once per machine)

```
py -3 -m pip install pyserial comtypes
```

(`comtypes` is only needed for `flash`; `adb` must already be on `PATH` for `capture`.)

## Which stages to run

- **Flash a device and collect fresh boot logs** -> `all --yes` (or `flash capture report --yes`)
- **Device already flashed with the build you want** -> `capture --build-path "\\swayam\...\performance"`
- **A capture flashed/booted fine but failed during the adb-pull/report step** -> `capture --resume-pull --target <target> --build-path <path>` (does not reflash or repeat boots)
- **Device stuck in EDL/Sahara after an aborted flash** -> `flash --recover`
- **Just want to re-render the HTML from existing JSON, or hand-append a run** -> `report --report-cmd render --target <target>` or `report --report-cmd add-run --run-json <file> --target <target>`

Run with the ARM64 Python launcher on this machine (`py -3 scripts/bootbench.py ...`), or a
regular Windows Python install (`python3 scripts/bootbench.py ...`). `flash` without `--yes`
pauses for a manual y/N confirmation right before it actually flashes -- pass `--yes` to skip
that in automated/hands-off runs.

## Hard rules

- **Never hand-edit** `Boot-Charts/bootchart-data-<target>.json` or
  `Boot-Charts/bootchart-overview-<target>.html`. The JSON is the only file ever written by
  hand (via `report add-run` or `capture`); the HTML is *always* regenerated from the JSON by
  this script, never hand-authored or hand-patched.
- Each metric's `hitters` list is **rank-ordered, not identity-ordered** -- array position 0
  is rank #1 for that boot. Do not assume a specific service holds a specific rank across
  different runs; which services appear and in what order changes between boots.
- Full details on the JSON schema, output paths, and the `hitters`/rank convention:
  [references/DATA_MODEL.md](references/DATA_MODEL.md).
- Full flag reference and worked command examples for every stage/combination:
  [references/COMMAND_REFERENCE.md](references/COMMAND_REFERENCE.md).

## Output locations

- `Boot-Charts/bootchart-data-<slug>.json` + `Boot-Charts/bootchart-overview-<slug>.html` --
  one pair per target (`<slug>` = target name with hyphens/underscores stripped).
- `Boot-Logs/<target>/<build-folder>/` -- raw per-boot log dumps (`Logs-1`, `Logs-2`, ...) plus
  permanent per-run `.txt` backups (unbounded history; the JSON/HTML only keep the most recent
  30 runs).

The report is written to disk but never auto-opened -- open the `.html` file yourself when
ready to view it.
