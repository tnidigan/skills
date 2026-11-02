---
name: report
description: >
  Use this skill when no device is involved and the user wants to
  maintain the Boot-Charts HTML report — regenerate it from the
  existing JSON history (`render`), append a run JSON manually to that
  history (`add-run`), or commit agent-authored
  outlier/regression/root-cause analysis text onto a recorded boot
  (`set-analysis`). Do not use this skill if the device needs to be
  flashed first (use `flash`) or if fresh boot numbers need to be
  collected from a flashed device (use `capture`) — both of those chain
  into `report.py` themselves. This skill is the standalone
  no-hardware-required entry point.
license: Internal use only.
compatibility: >
  No device/hardware dependency — `report.py` has none. Pure Python
  standard library, runs anywhere `py -3` / `python3` is available
  (Windows, Linux, macOS). No `pyserial` / `comtypes` / `adb` / PCAT /
  nightly-share access needed. The only inputs are the on-disk
  `bootchart-data-<slug>.json` and (for `add-run` / `set-analysis`) a
  run JSON or two text files.
metadata:
  author: "Tarun Balaji Nidiganti"
  version: "1.0.0"
  platform: "Qualcomm Linux target device (e.g. iq-9075-evk) — reporting only, no device access"
  category: "Boot Performance / Reporting"
  entry-point: "report/report.py"
  chains-into: "nothing (terminal stage)"
---

Maintain `bootchart-data-<slug>.json` (the only hand-edited file — by
appending a run, or by annotating a boot with analysis text) and
regenerate `bootchart-overview-<slug>.html` from it. The HTML is
**always** fully derived from the JSON — never hand-authored. Re-running
`render` regenerates it byte-for-byte from whatever is currently in the
JSON.

`report.py` has no cross-module imports at all — unlike `capture.py`, it
has no dependency on where `flash.py` / `regression_analysis.py` are
deployed. It's self-contained and runs anywhere Python 3 does.

## When to use this skill

Use this skill when:
- The HTML report is out of sync with the JSON (edited by hand, or
  rendered by an older version of `report.py`) and needs a clean
  regeneration → `--report-cmd render`
- A run JSON produced out-of-band (e.g. a prior capture that wrote the
  run JSON but failed before the record step) needs to be appended to
  the history → `--report-cmd add-run`
- An agent has written `overall_overheads_text` and/or
  `optimization_possibilities` prose into text files and wants it
  committed onto a boot in the most recent run, with the HTML
  re-rendered in the same step → `--report-cmd set-analysis`

Do NOT use this skill when:
- The device needs to be (re)flashed first → use `flash`
  (`../flash/SKILL.md`)
- Fresh boot-time numbers need to be collected from an already-flashed
  device → use `capture` (`../capture/SKILL.md`), which chains into
  `report.py --report-cmd add-run` automatically

Example prompts:
- "Regenerate the boot report HTML from the existing JSON"
- "Append this run JSON to the boot report for iq-9075-evk"
- "Write the overheads and optimization analysis onto default-1"
- "Commit the agent's RCA writeup onto the latest debug-1 boot"
- "The dashboard looks stale — re-render it"

---

## Inputs

All inputs are CLI flags on `report.py`. `--target` is always required
(it resolves which `bootchart-data-<slug>.json` /
`bootchart-overview-<slug>.html` pair to operate on). Other flags are
mode-specific — see the table below for which flag applies to which
`--report-cmd`.

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `--target` | string | **Yes (always)** | — | Target name, e.g. `iq-9075-evk`. Used to resolve `bootchart-data-<slug>.json` / `bootchart-overview-<slug>.html`. |
| `--report-cmd` | choice: `render`, `add-run`, `set-analysis` | No | `render` | Which operation to perform (see Workflow below) |
| `--run-json` | path or `-` | Only with `--report-cmd add-run` | — | Path to a run JSON to append (or `-` for stdin) |
| `--log-name` | string | Only with `--report-cmd set-analysis` | — | `log_name` of the boot to annotate (e.g. `default-1`, `debug-1`) in the **most recently recorded run** |
| `--overheads-file` | path | At least one of these two is required with `--report-cmd set-analysis` | — | Text file whose UTF-8 contents become the named boot's `overall_overheads_text` (no templating/interpolation) |
| `--optimization-file` | path | At least one of these two is required with `--report-cmd set-analysis` | — | Text file whose UTF-8 contents become the named boot's `optimization_possibilities` |

`--log-name` always targets the latest run — log names aren't unique
across history, so there's no way to annotate an older run. If you need
to edit an older run, edit the JSON directly and re-run with
`--report-cmd render`.

---

## Workflow

The three `--report-cmd` modes are independent sub-workflows. Pick the
one that matches what the user asked for; nothing cascades.

### Mode 1: `render` (default)

1. Load `bootchart-data-<slug>.json` from disk.
2. Walk every run → every boot → render the device-info table, boot
   metrics table, `regression_analysis` dict, and the two prose fields
   (`overall_overheads_text`, `optimization_possibilities`).
3. Write `bootchart-overview-<slug>.html` byte-for-byte derived from
   the JSON. No other side effects.

### Mode 2: `add-run`

1. Read the run JSON from `--run-json` (or stdin if `-`).
2. Append it to `bootchart-data-<slug>.json`'s run list.
3. Write the updated JSON back to disk.
4. Immediately re-render the HTML (same as `render` above).

This is how `capture.py` records its result — the on-disk run JSON
`capture.py` builds is passed straight to `--report-cmd add-run`.

### Mode 3: `set-analysis`

1. Load `bootchart-data-<slug>.json`.
2. Locate the **most recently recorded run** (last entry in the run
   list). Older runs are never touched.
3. Within that run, find the boot whose `log_name` matches `--log-name`
   (e.g. `default-1`, `debug-1`). If no match, **fail loudly** and list
   the available `log_name`s in that run — don't silently do nothing.
4. Read `--overheads-file` and/or `--optimization-file` as UTF-8 (no
   templating, no interpolation) and write their contents into that
   boot's `overall_overheads_text` and/or `optimization_possibilities`
   fields respectively, overwriting the "Not yet analyzed" placeholders
   `capture.py` left there.
5. Write the updated JSON back to disk.
6. Immediately re-render the HTML.

This is the write side of the workflow `capture/SKILL.md` describes:
`capture.py` populates each displayed boot's `regression_analysis`
dict (raw findings) plus the two `"Not yet analyzed"` placeholders; an
agent reads the raw findings, writes real prose to two text files
following `../capture/reference/COT_outlier_detection.md`,
`COT_regression_rca.md`, and `COT_optimization_recommendations.md`,
then calls `set-analysis` to commit that prose into the JSON and
re-render the HTML — one command, no separate render step afterward.

```bash
# Mode 1: regenerate HTML from existing JSON
py -3 report.py --target iq-9075-evk
py -3 report.py --report-cmd render --target iq-9075-evk

# Mode 2: append a run JSON, then render
py -3 report.py --report-cmd add-run --target iq-9075-evk \
    --run-json new_run.json

# Mode 2 via stdin (e.g. piped from a script)
cat new_run.json | py -3 report.py --report-cmd add-run \
    --target iq-9075-evk --run-json -

# Mode 3: commit agent prose onto default-1 in the latest run
py -3 report.py --report-cmd set-analysis --target iq-9075-evk \
    --log-name default-1 \
    --overheads-file overheads.txt --optimization-file optimization.txt

# Mode 3: only write the overheads field (optimization stays untouched)
py -3 report.py --report-cmd set-analysis --target iq-9075-evk \
    --log-name debug-1 --overheads-file overheads.txt
```

### Rendering contract for the two analysis fields

`overall_overheads_text` and `optimization_possibilities` both render
through the same plain bullet-per-line HTML helper (one `<li>` per
newline in the source text, or a single `<div>` for one-paragraph
text) — deliberately no tables, charts, or nested structures, so the
dashboard stays simple regardless of how much analysis text is behind
it. Older entries captured before `overall_overheads_text` existed fall
back to the legacy pooled-hitters list rendering automatically — no
migration needed.

---

## Outputs

| Output | Description |
|---|---|
| `bootchart-overview-<slug>.html` | Always rewritten (every mode re-renders in its final step). Fully derived from the JSON; safe to delete and regenerate at any time. |
| `bootchart-data-<slug>.json` | Rewritten by `add-run` (new run appended) and `set-analysis` (two prose fields updated on one boot). Untouched by `render`. |
| `bootchart-data-<slug>.json.<timestamp>.bak.txt` | `.txt` backup of the previous JSON state, written by `add-run` and `set-analysis` right before they modify the JSON — see `report.py`'s module docstring for the exact format. `render` never writes a backup since it doesn't touch the JSON. |
| Exit code | `0` on success; non-zero (with a stderr message listing available `log_name`s) if `set-analysis` can't find the requested boot in the latest run. |

The HTML and JSON paths both live next to `report.py` in whatever
`Boot-Charts/` folder the deployment uses — see the root `../SKILL.md`'s
"Repository Structure" for the layout.

---

## Error Handling

- **`--target` missing** → rejected at argument-parsing time (always
  required, in every mode).
- **`bootchart-data-<slug>.json` doesn't exist** on `render` or
  `set-analysis` → fails with the resolved path; `add-run` creates it
  from scratch with the single appended run as the only entry.
- **`--report-cmd add-run` without `--run-json`** → rejected at
  argument-parsing time.
- **`--run-json` points to invalid JSON** → fails before writing
  anything, so the existing `bootchart-data-<slug>.json` is left
  untouched.
- **`--report-cmd set-analysis` with neither `--overheads-file` nor
  `--optimization-file`** → rejected: at least one must be given, since
  the command would otherwise be a no-op.
- **`--log-name` doesn't match any boot in the latest run** → fails
  loudly, prints the list of `log_name`s that *do* exist in that run,
  and exits non-zero — never silently writes to the wrong boot or does
  nothing.
- **`--overheads-file` / `--optimization-file` path doesn't exist or
  isn't UTF-8** → fails before writing anything; the JSON is left
  untouched.
- **Trying to annotate an older run** → not supported by this skill. Edit
  the JSON directly and re-run `--report-cmd render` to pick up the
  change.

## Reference

Full technical detail (the run-object JSON schema, device-info table,
the `.txt` backup format, the exact HTML helper for the two prose
fields, and the legacy pooled-hitters fallback) lives in `report.py`'s
own module docstring and inline comments — read it directly for
implementation-level questions this file doesn't answer. For the
agent-authored prose that `set-analysis` commits, see the three COT
docs under `../capture/reference/`.
