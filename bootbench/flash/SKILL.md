---
name: flash
description: >
  Use this skill when a Qualcomm target device needs to be flashed with a
  nightly Yocto build over EDL via PCAT - finding the latest build (or using
  one the user specifies), detecting the target name and on-board memory
  type (UFS/EMMC) over the serial console, entering EDL mode via the Alpaca
  TAC, and flashing via PCAT. Also use this when the user wants the full
  flash -> capture -> report pipeline in one command (`--all`), or needs to
  recover a device stuck in EDL/Sahara mode. Do not use this skill if the
  device is already flashed and the user only wants fresh boot-time data
  (use `capture` instead) or only wants the HTML report regenerated with no
  device involved (use `report` instead).
license: Internal use only.
compatibility: >
  Windows host, run with this machine's ARM64 Python launcher (`py -3`) or
  a regular Windows Python 3 install (`python3`). Requires
  `pip install comtypes pyserial`. Requires physical access to: the Alpaca
  TAC debug board (power/EDL control, via `comtypes` COM automation), a
  serial (COM) console connection to the target device, and Qualcomm PCAT
  installed at `C:\Program Files (x86)\Qualcomm\PCAT\bin\PCAT.exe`. Requires
  network access to the nightly build share (`\\swayam\QLI_Builds\Yocto`).
metadata:
  author: "Tarun Balaji Nidiganti"
  version: "1.0.0"
  platform: "Qualcomm Linux target device (e.g. iq-9075-evk)"
  category: "Device Flashing / EDL"
  entry-point: "flash/flash.py"
  chains-into: "capture (via --all), report (transitively, via capture)"
---

Flash a Qualcomm target device with a nightly build over EDL via PCAT.
Resolves the build, detects the target/memory type, enters EDL mode via the
Alpaca TAC, and flashes. This is a complete, standalone skill — it does not
require `capture.py`/`report.py` to be present at all unless `--all` is
explicitly passed, in which case it chains straight into boot-time capture
and reporting.

## When to use this skill

Use this skill when:
- A target device needs to be (re)flashed with a nightly build
- The user wants to flash a specific build/target/memory-type combination
- The user wants to recover a device stuck in EDL/Sahara mode after a
  previous failed flash
- The user wants the entire flash → capture → report pipeline run in one
  command

Do NOT use this skill when:
- The device is already flashed and the user just wants fresh boot-time
  numbers from it → use `capture` (`../capture/SKILL.md`)
- No device is involved at all; the user just wants the existing HTML
  report regenerated or a run JSON appended → use `report`
  (`../report/SKILL.md`)

Example prompts:
- "Flash the device with the latest nightly build"
- "Flash iq-9075-evk with UFS memory, skip the confirmation prompt"
- "Flash and give me the boot-time report" (→ use `--all`)
- "The device is stuck in EDL mode, recover it"
- "Dry-run a flash so I can see what build/target it would use"

---

## Inputs

All inputs are CLI flags on `flash.py`. None are strictly required — every
value is auto-detected if omitted, except where noted.

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `--target` | string | No | auto-detected via serial (`uname -n`) | Target name, e.g. `iq-9075-evk` |
| `--com-port` | string | No | auto-detected by probing all COM ports | Serial console COM port |
| `--boot-timeout` | int | No | `480` | Seconds to wait for a login prompt (first boot and after each reboot) |
| `--tac-port` | string | No | auto-detected if exactly one TAC device | Alpaca TAC COM port name, e.g. `VTP8` |
| `--memory-type` | choice: `UFS`, `EMMC` | No | auto-detected via `lsblk` over serial | On-board storage type passed to PCAT's `-MEMORYTYPE` |
| `--dry-run` | flag | No | off | Resolve build/target/memory-type and print the plan; stop before touching the device |
| `--yes` | flag | No | off | Skip the y/N confirmation prompt right before flashing |
| `--skip-edl` | flag | No | off | Device is already in EDL mode; skip the TAC `BootToEDL` step |
| `--recover` | flag | No | off | Power-cycle the device via TAC (out of EDL/Sahara back to normal boot) and exit immediately. Mutually exclusive with `--all` |
| `--all` | flag | No | off | After a successful flash, chain into `capture.py` then `report.py` in-process, reusing the target/build-path/COM port this flash just resolved |

When `--all` is given, `capture.py`'s and `report.py`'s own flags
(`--build-path`, `--num-boots`, `--resume-pull`, `--report-cmd`,
`--run-json`) are also accepted on the same command line — but only
`--num-boots` is actually meaningful in that path; the rest don't apply
once flash has already determined the build path and target for this run.

**Mutually exclusive:** `--recover` and `--all` cannot be combined.

`--all` is optional convenience, not a dependency: a bare `flash.py`
invocation (no `--all`) never imports `capture.py`/`report.py`, so this
skill works standalone even when they're not deployed alongside it. When
`--all` *is* passed, `flash.py` locates `capture.py`/`report.py` whether
they're sitting next to `flash.py` or in their own sibling `capture/`/
`report/` skill folders (see the root `SKILL.md`'s "Cross-module imports"
note) — if neither layout has them, `--all` fails fast with a clear error
instead of a bare `ModuleNotFoundError`.

---

## Workflow

1. **Handle `--recover` first, if given.** Power-cycle the device via the
   Alpaca TAC and exit — no build discovery, no flashing.
2. **Find the latest nightly build.** Scan `\\swayam\QLI_Builds\Yocto` for
   folders matching `..._Nightly_Build_master_<N>` and pick the highest
   `<N>`.
3. **Resolve target + memory type.**
   - If both `--target` and `--memory-type` are given explicitly, skip
     opening a serial console entirely.
   - Otherwise, power-cycle the device via TAC for a known-good boot
     state, open the serial console (auto-detecting the COM port if
     `--com-port` wasn't given), log in, and:
     - Run `uname -n` to detect the target name (unless `--target` given)
     - Run `lsblk` to classify on-board storage as `UFS` (top-level
       `sda`/`sdb`/... SCSI disks) or `EMMC` (top-level `mmcblk0` +
       `mmcblk0boot0`/`boot1`) (unless `--memory-type` given)
4. **Resolve the build folder** —
   `<latest_build>\performance\qcom-multimedia-proprietary-image-<target>`
   — and confirm it's a valid flat build by checking for `rawprogram0.xml`.
5. **If `--dry-run`:** print the resolved build dir, target, and memory
   type, and stop before touching the device.
6. **Enter EDL mode** via the Alpaca TAC's `BootToEDL` (unless
   `--skip-edl`), wait for the device to re-enumerate, and discover it via
   `PCAT -DEVICES`.
7. **Confirm with the user** (unless `--yes`) showing device ID, build dir,
   memory type, and slot, then flash via
   `PCAT -PLUGIN SD -DEVICE <id> -BUILD <dir> -MEMORYTYPE <type> -SLOT 0`.
   - Ctrl+C during the flash kills PCAT and automatically power-cycles the
     device back to normal boot.
8. **If `--all`:** immediately continue into `capture.py`'s
   `capture_and_record(...)` in the same process, reusing the
   target/build-path/COM port just resolved — which itself records and
   renders the report, so no separate report step runs.

```bash
# Flash only -- waits for a manual y/N confirmation right before flashing
py -3 flash.py

# Flash with explicit target/memory-type, skip confirmation
py -3 flash.py --target iq-9075-evk --memory-type UFS --yes

# Resolve the plan without touching the device
py -3 flash.py --dry-run

# Flash, then capture + report in one command
py -3 flash.py --all --yes

# Flash, then capture 5 boots per phase, then report
py -3 flash.py --all --num-boots 5 --yes

# Recover a device stuck in EDL/Sahara mode
py -3 flash.py --recover
```

---

## Outputs

| Output | Description |
|---|---|
| Flashed device | The target device, reflashed via PCAT with the resolved/overridden build + memory type |
| Return value (`cmd_flash`) | `{target, build_path, com_port}` dict — `None` if the pipeline stopped early (`--recover`, `--dry-run`, or declined confirmation) |
| *(only with `--all`)* `Boot-Charts/`, `Boot-Logs/` | Produced by the chained `capture.py`/`report.py` run — see `report/SKILL.md` for their shape, once that usecase is written up |

The flash step itself writes nothing to disk beyond what PCAT writes to
the device; there is no flash-specific JSON/HTML artifact.

---

## Error Handling

- **No EDL device found** after `BootToEDL` within the poll timeout → the
  error message lists the last-seen PCAT device list; check the USB/debug
  board connection.
- **PCAT flash fails** (non-zero exit) → raises with PCAT's exit code; the
  device may be left in EDL mode — run `--recover` or retry.
- **Ctrl+C during the flash** → PCAT is killed and the device is
  automatically power-cycled back to normal boot; if that power-cycle
  itself fails, the error says the device may still be in EDL/Sahara mode
  and suggests `--recover` or the TAC app / physical reset.
- **No live serial console found** on any COM port within the boot
  timeout → pass `--com-port` explicitly, or check the device is powered
  on.
- **Multiple TAC devices found** with no `--tac-port` given → pass
  `--tac-port` to disambiguate.
- **Build folder doesn't look like a flat build** (missing
  `rawprogram0.xml`) → the target name is likely wrong, or that nightly
  build doesn't have a build for this target.
- **`--recover` combined with `--all`** → rejected at argument-parsing time
  (mutually exclusive).

## Reference

Full technical detail (regex patterns, serial-console quirks, TAC COM
automation internals) lives in `flash.py`'s own module docstring and inline
comments — read `flash/flash.py` directly for implementation-level
questions this file doesn't answer.
