---
name: bootbench
description: Flashes a Qualcomm EDL device via PCAT with the latest Yocto nightly build, captures boot-time metrics (systemd-analyze, dmesg, journalctl) across a default-cmdline phase and a debug-cmdline phase, extracts kernel probe/initcall overheads from debug dmesg, and generates a compact JSON+HTML boot-time report with cross-stage overhead ranking. Use when the user asks to flash a device and benchmark its boot time, capture boot logs, re-render a boot-time report, or investigate boot-time regressions/overheads for a target like iq-9075-evk.
compatibility: Windows only. Requires PCAT, an Alpaca TAC debug board, pyserial, comtypes, and adb on PATH.
---

# bootbench

`scripts/bootbench.py` is the single entry point. It runs up to three
stages, always in this fixed order regardless of how they're listed on the
command line:

| Stage | Responsibility |
|---|---|
| `flash` | Finds the latest Yocto nightly build, detects the target device over serial, flashes it via PCAT over EDL |
| `capture` | Logs into the serial console, runs a two-phase boot loop (default-cmdline, then debug-cmdline), pulls logs via `adb`, parses them, records+renders the report |
| `report` | Standalone JSON/HTML maintenance (re-render HTML, or append a run) — not needed after `capture`, which already does this itself |

`all` is shorthand for `flash capture report`.

## Setup (one-time)

```
py -3 -m pip install pyserial comtypes
```

`adb` must also be on `PATH`. `pyserial` is needed for `flash` and
`capture`; `comtypes` only for `flash` (Alpaca TAC automation).

## Which command to run

- Fresh flash + full benchmark: `py -3 scripts/bootbench.py all --yes`
- Device already flashed, just capture: `py -3 scripts/bootbench.py capture --build-path "\\swayam\...\performance"`
- A capture's flash/boots succeeded but the `adb pull`/report step failed: `py -3 scripts/bootbench.py capture --resume-pull --target <target> --build-path <path>`
- Device stuck in EDL/Sahara after an aborted flash: `py -3 scripts/bootbench.py flash --recover`
- Just re-render the HTML from existing JSON (no device involved): `py -3 scripts/bootbench.py report --report-cmd render --target <target>`
- Manually append a run JSON: `py -3 scripts/bootbench.py report --report-cmd add-run --run-json <path or -> --target <target>`

Use `py -3` on the ARM64 Python launcher on this machine, or `python3` on a
regular Windows Python install.

See `references/COMMAND_REFERENCE.md` for the complete flag list and more
examples.

## Workflow

Follow this sequence any time this skill is activated:

1. **Run the device stage(s) the user asked for** — `flash`, `capture`, or
   `all` — per "Which command to run" above. `capture` records the run and
   renders the HTML itself; no separate `report` call is needed right
   after it.
2. **Open the just-updated `Boot-Charts/bootchart-data-<slug>.json`** and
   find the run you just captured (it's the last element of `runs`). Read
   its `boots` entries.
3. **Review each boot's `overall_overheads`** — this is the pooled,
   re-ranked top-3 list of the actual biggest individual time costs for
   that boot (a specific slow probe, a specific slow service, ...), drawn
   from the Kernel/sysinit/multi-user hitters. On the Debug boot, also
   check the Kernel row's own `hitters` (top slow probes/initcalls, plus
   any probe/initcall failures) for extra root-cause detail.
4. **Write real, concrete optimization suggestions** for that run based on
   what you found — not generic advice, and not one paragraph. Write it as
   a point-wise list: **one bullet per hitter** you're discussing (each
   service/probe/unit named in `overall_overheads`, plus any debug-boot
   kernel `hitters` worth calling out). Each bullet should:
   - Start with `• ` followed by the unit/service/probe name and its time
     (e.g. `• libvirtd.service (2.362 s): ...`).
   - Briefly explain why it's likely costing boot time.
   - Propose a specific fix (e.g. "consider socket-activating it or moving
     it out of the boot-critical path with `systemd.wants`" or "check for
     a synchronous link-training wait that could run asynchronously").
   - Be separated from the next bullet by a newline (`\n`), so each point
     renders on its own line rather than running together.
   Do this for every boot in the run that has a non-null-worthy
   `overall_overheads` list (default and debug alike, whenever there's a
   real hitter to speak to).
5. **Write the result into that boot's `optimization_possibilities`
   field** in the JSON, as a single string with each bullet point on its
   own line (`\n`-separated), replacing the placeholder string (or
   `null`) with your concrete analysis. This is the one sanctioned
   exception to the "never hand-edit the JSON" rule below — this field
   exists specifically to be filled in by an agent after reviewing
   `overall_overheads`; nothing else in the file should change.
6. **Re-render the HTML** so the report reflects what you wrote:
   `py -3 scripts/bootbench.py report --report-cmd render --target <target>`.
7. Tell the user what you found and where to look — don't just silently
   edit the file.

See `references/DATA_MODEL.md` for the full JSON schema, including the
`overall_overheads` / `optimization_possibilities` fields and the kernel
row's debug-derived `hitters`.

## Hard rules

- Never hand-edit `bootchart-data-<slug>.json` directly, **except** for
  writing a run's `optimization_possibilities` field per the Workflow
  above — that field is the one part of the schema an agent is expected to
  fill in by hand. Everything else in the JSON is modified only via the
  `capture` stage or `report --report-cmd add-run`.
- Never hand-edit `bootchart-overview-<slug>.html`. It is always fully
  regenerated from the JSON by `render()` — re-run `report
  --report-cmd render` after any JSON change, including an
  `optimization_possibilities` edit.
- A `hitters` list (on the Kernel, sysinit, or multi-user rows, and on the
  pooled Overall Suspected Overheads row) is rank-ordered for that boot,
  not an identity list — the same unit can appear at a different rank, or
  not at all, on a different boot.
- The debug phase's cmdline edit is **never reverted** once applied to a
  device — it persists across future `capture`-only runs against the same
  device, even without reflashing.

## Output locations

- `Boot-Charts/bootchart-data-<slug>.json` + `bootchart-overview-<slug>.html`
  — one pair per target, `<slug>` = target name with hyphens/underscores
  stripped. Keeps the most recent 30 runs.
- `Boot-Logs/<target>/<build-folder>/` — every captured boot's raw log dump
  (`Logs-default-1..N`, `Logs-debug-1..N`) plus permanent per-boot `.txt`
  backups (unbounded history, unlike the JSON/HTML's 30-run cap).
- The HTML report is written to disk but not auto-opened — open it
  yourself when ready.
