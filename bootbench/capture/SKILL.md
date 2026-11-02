---
name: capture
description: >
  Use this skill when a Qualcomm target device is already flashed and
  the user wants fresh boot-time numbers from it — boot the device N
  times (default: 3) under the default kernel cmdline, then N more with
  debug cmdline params added, pull the resulting logs via adb, parse
  them into structured metrics, run outlier/regression/root-cause
  analysis across the sibling boots and against the previous build, and
  record + render the HTML report (via report.py). Also use this to
  resume a prior capture that reached adb pull and failed partway
  (`--resume-pull`). Do not use this skill if the device needs to be
  (re)flashed first (use `flash` instead) or if no device is involved
  and the user only wants the HTML regenerated / a run JSON appended
  (use `report` instead).
license: Internal use only.
compatibility: >
  Windows host, run with this machine's ARM64 Python launcher (`py -3`)
  or a regular Windows Python 3 install (`python3`). Requires
  `pip install pyserial`, plus `adb` on `PATH` to pull logs off the
  device. Requires a serial (COM) console connection to the target for
  login and reboots. Does not require `comtypes` or PCAT (no EDL/flash
  path), but must be deployed alongside `flash.py` and `report.py`
  because `capture.py` delegates to them for the serial console and
  report rendering respectively.
metadata:
  author: "Tarun Balaji Nidiganti"
  version: "1.0.0"
  platform: "Qualcomm Linux target device (e.g. iq-9075-evk)"
  category: "Boot Performance"
  entry-point: "capture/capture.py"
  chains-into: "report (via report.py, in-process; always, not optional)"
---

Boot an already-flashed Qualcomm target device N times across two
phases (default-cmdline, debug-cmdline), collect per-boot timing and
failure data, run outlier/regression/root-cause analysis, and hand the
result to `report.py` to record and render — all without reflashing.

The underlying script (`capture.py`) is a standalone entry point. It
does a plain `import flash`, `import report`, and `import
regression_analysis` — it has no serial-console or report-rendering
logic of its own and delegates entirely to those modules — so it must
be deployed alongside `flash.py`, `report.py`, and
`regression_analysis.py`, either flat in the same folder or each in its
own sibling skill folder (`flash/`, `capture/`, `report/`).
`regression_analysis.py` lives in this same `capture/` folder and is
imported directly (no `_ensure_importable` shim needed — it's always
co-located with `capture.py`). See the root `../SKILL.md`'s
"Cross-module imports" note for how `capture.py` locates
`flash`/`report` under either layout.

## When to use this skill

Use this skill when:
- The device is already flashed and the user wants fresh boot-time
  numbers recorded + reported
- The user explicitly wants more (or fewer) than the default 3 boots
  per phase
- A prior capture reached the adb-pull stage and failed partway, and
  the user wants to resume by pulling the on-device logs rather than
  rebooting from scratch (`--resume-pull`)

Do NOT use this skill when:
- The device needs to be (re)flashed first → use `flash`
  (`../flash/SKILL.md`), which can chain into capture via `--all`
- No device is involved at all — the user just wants the existing HTML
  report regenerated, a run JSON appended, or agent-authored analysis
  text committed → use `report` (`../report/SKILL.md`)

Example prompts:
- "Device is already flashed, just give me fresh boot numbers"
- "Capture 5 boots per phase and record them in the report"
- "Run a boot capture against the current build"
- "A prior capture failed during adb pull — resume it, don't reboot"
  (→ use `--resume-pull`)

---

## Inputs

All inputs are CLI flags on `capture.py`. Nothing is strictly required
for a normal capture run — the target, build path, and COM port are
auto-detected if omitted. `--resume-pull` is the one exception: it
requires `--target` and `--build-path` explicitly, since there's no
serial-console step to auto-detect them from.

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `--target` | string | Only with `--resume-pull` | auto-detected via serial (`uname -n`) | Target name, e.g. `iq-9075-evk` |
| `--com-port` | string | No | auto-detected by probing all COM ports | Serial console COM port |
| `--boot-timeout` | int | No | `480` | Seconds to wait for a login prompt (first boot and after each reboot) |
| `--build-path` | string | Only with `--resume-pull` | latest nightly build under `\\swayam\QLI_Builds\Yocto` | Nightly build path this boot used, e.g. `\\swayam\...\performance` — recorded into the run JSON so the report can show which build this run came from |
| `--num-boots` | int | No | `3` | Boots per phase. `--num-boots 3` → 3 default-cmdline + 3 debug-cmdline = 6 total. Values < 1 silently fall back to 3. |
| `--resume-pull` | flag | No | off | Skip login/boot-loop entirely: adb is already enabled and all boots' `/data/Logs-<n>` are already on the device from a prior failed run — just pull logs and (re)build the report. Requires `--target` and `--build-path`. |

Only the displayed `default-1` and `debug-1` boots are shown in the
report table, but all `2 × --num-boots` boots feed the regression /
outlier / high-hitter analysis (see Workflow step 5).

---

## Workflow

1. **Resolve target + build + COM port.**
   - If `--resume-pull`: use `--target` and `--build-path` directly, skip
     all serial-console steps and go to step 4.
   - Otherwise: open the serial console (auto-detecting the COM port if
     `--com-port` wasn't given), log in, run `uname -n` to detect the
     target (unless `--target` given), and resolve the latest nightly
     build (unless `--build-path` given).
2. **Phase 1: default-cmdline boots.** For each of `--num-boots` boots:
   - Reboot the device from the serial console
   - Wait for login prompt (up to `--boot-timeout` seconds)
   - Enable adb, collect `/proc/cmdline` to tag the phase
   - Capture boot-time data into `/data/Logs-<n>` on the device
3. **Phase 2: debug-cmdline boots.** Inject debug cmdline params
   (`initcall_debug`, probe-timing flags, etc.) via the serial console,
   then repeat step 2 for another `--num-boots` boots. The debug cmdline
   is what makes kernel-probe hitters observable in these boots'
   `dmesg.txt`.
4. **adb pull.** For each of the `2 × --num-boots` boots, `adb pull`
   `/data/Logs-<n>` onto the host. If the pull step was already
   partially done (prior run failure), `--resume-pull` jumps straight
   here.
5. **Parse + analyze.**
   - `parse_failed_units()` extracts every unit `systemctl` reports as
     `ACTIVE=failed` from each boot's `blame_systemdsystemctl.txt` and
     stores it as `boot["metrics"]["failed_units"]` (alongside the
     kernel-probe-failure entries already tracked in debug boots'
     `metrics.kernel.hitters`, tagged with `note` in
     `("probe/initcall failure", "probe failure")`).
   - `regression_analysis.build_analysis(...)` runs **once** across all
     `2 × --num-boots` boots plus the target's prior JSON history
     (loaded *before* the new run is appended — see
     `regression_analysis.py`'s own module docstring for why ordering
     matters). It produces:
     - `outliers` — per-phase, each tagged `kind: "duration"` (MAD-based,
       vs. sibling boots) or `kind: "status_divergence"` (failed in some
       sibling boots but not others)
     - `intra_run_regressions` — named units that regressed >15% vs.
       their sibling-boot baseline within this same capture
     - `previous_build_regressions` — named units that regressed vs. the
       equivalent phase/boot in the target's previous recorded build
     - `high_hitters` — per displayed boot,
       `{"ranked": [...], "failures": [...]}` — biggest-duration named
       units plus any units/probes that failed in *every* sibling boot
       of that phase (deliberately excluded from `outliers`, since
       "always failed" has no passing sibling to diverge against)
     - `unit_appeared_once` — units present in only 1 of a phase's N
       sibling boots; carries `missing_from` and `raw_log_hint` for
       manual investigation
6. **Attach to displayed boots.** Both `default-1` and `debug-1` get the
   **same** `regression_analysis` dict attached
   (`boot["regression_analysis"] = analysis`), plus two placeholder
   free-text fields that `capture.py` itself never fills in with real
   prose:
   - `overall_overheads_text`:
     `"Not yet analyzed — ask an agent to review the regression_analysis data above."`
   - `optimization_possibilities`:
     `"Not yet analyzed — ask an agent to review the overheads above and suggest fixes."`
7. **Hand off to report.py.** Call `report.py --report-cmd add-run`
   in-process with the newly-built run JSON — this appends it to
   `bootchart-data-<slug>.json` and re-renders
   `bootchart-overview-<slug>.html`. Capture never writes HTML itself.

```bash
# Standard capture against the latest nightly, 3 boots per phase (6 total)
py -3 capture.py

# Explicit build path (e.g. match what flash just used)
py -3 capture.py --build-path "\\swayam\...\performance"

# More statistical confidence -- 5 boots per phase (10 total)
py -3 capture.py --num-boots 5

# Resume a prior run that died during adb pull
py -3 capture.py --resume-pull --target iq-9075-evk \
    --build-path "\\swayam\...\performance"
```

## Turning the placeholders into real analysis (agent task)

Step 6 above only writes the "Not yet analyzed" placeholders —
`capture.py` deliberately does not generate prose. After a capture
completes, an **agent** should:

1. Read the raw findings from `regression_analysis` (reachable via the
   recorded JSON, or directly from the `build_analysis()` return value
   right after a fresh capture).
2. Follow `reference/COT_outlier_detection.md` to write the full
   `overall_overheads_text` — outliers → intra-run regressions →
   previous-build regressions → high hitters → root-cause analysis
   (five subsections, always in that order).
3. Follow `reference/COT_regression_rca.md` for the actual root-cause
   methodology that feeds subsection (v) above — correlate
   `journalctl.log` / `dmesg.txt` / `critical_chain.txt` evidence into a
   symptom / evidence / category / confidence / next-steps writeup per
   finding. Never claim a root cause without quoted evidence, and never
   propose a fix here.
4. Follow `reference/COT_optimization_recommendations.md` to write
   `optimization_possibilities` — fix/strategy text only, one
   recommended approach per RCA'd finding (or an explicit "re-run to
   confirm" / "investigate further, no fix yet" when the RCA step
   wasn't confident).
5. Commit both prose fields with `report.py --report-cmd set-analysis`
   (see `../report/SKILL.md`) — this writes the two files' contents
   into the latest run's named boot and re-renders the HTML in the same
   step.

See `reference/` in this folder for the three COT docs in full.

### Keep the dashboard simple

Both `overall_overheads_text` and `optimization_possibilities` render
through the same plain bullet-per-line HTML helper — no tables, no
charts, no nested structures. Write them as flat prose with
numbered/bulleted findings, not structured data.

---

## Outputs

| Output | Description |
|---|---|
| `bootchart-data-<slug>.json` | Appended with a new run object containing `2 × --num-boots` boot entries, their parsed metrics, and the shared `regression_analysis` dict on the two displayed boots. Lives next to `report.py`. |
| `bootchart-overview-<slug>.html` | Re-rendered by `report.py` from the updated JSON — never hand-authored. |
| `Boot-Logs/<run>/Logs-<n>/` | Raw per-boot logs pulled from the device (`blame.txt`, `critical_chain.txt`, `blame_systemdsystemctl.txt`, `dmesg.txt`, `journalctl.log`, `cmdline.txt`). Preserved on disk for the agent's RCA step. |
| Return value (`cmd_capture`) | Prints the final report path to stdout; propagates `report.py`'s rendering exit code. |

The two "Not yet analyzed" placeholder fields on each displayed boot
(`overall_overheads_text`, `optimization_possibilities`) are also
outputs in the sense that they *will* appear in the HTML immediately —
the agent task above is what replaces them with real prose. Until that
happens, the dashboard shows the placeholder text verbatim.

---

## Error Handling

- **No live serial console found** on any COM port within the boot
  timeout → pass `--com-port` explicitly, or check the device is
  powered on and the cable is seated.
- **Login prompt never appears** within `--boot-timeout` seconds after
  a reboot → the device may have failed to boot; the stage reports
  which boot number failed and leaves the earlier boots' logs on-device
  so `--resume-pull` can still salvage the completed boots after a
  manual reboot.
- **adb pull fails** (device dropped, path missing) → the stage reports
  which boot's `/data/Logs-<n>` failed. If some boots pulled
  successfully, re-run with `--resume-pull --target <...> --build-path
  <...>` to retry only the pull step without re-booting.
- **`--resume-pull` without `--target` or `--build-path`** → rejected
  at argument-parsing time (the normal auto-detect path is skipped, so
  these can't be inferred).
- **`report.py` fails during the final record/render** → the run JSON
  is still written to disk; re-run `report.py --report-cmd add-run
  --run-json <path>` manually to recover.
- **`--num-boots` < 1** → silently falls back to 3 (no error).

## Reference

Full technical detail (two-phase boot-loop state machine, debug cmdline
injection, `blame_systemdsystemctl.txt` parsing, MAD-based outlier
math, ordering of history load vs. new-run append) lives in
`capture.py`'s and `regression_analysis.py`'s module docstrings and
inline comments — read them directly for implementation-level questions
this file doesn't answer. The three COT docs in `reference/` are the
source of truth for the agent-authored prose step.
