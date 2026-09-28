# Data Model

## Source of truth

`Boot-Charts/bootchart-data-<slug>.json` (`<slug>` = target name with
hyphens/underscores stripped) is normally only ever changed via the
`capture` stage or `report --report-cmd add-run` — never hand-edit it
directly, with exactly one sanctioned exception: an agent filling in a
boot's `optimization_possibilities` field per the Workflow in `SKILL.md`.
No other field should be hand-edited.

`Boot-Charts/bootchart-overview-<slug>.html` is **entirely** generated from
that JSON by `render()`. Never hand-edit it — re-run `report
--report-cmd render` after any JSON change to regenerate it exactly.

## Run entry schema

Each element of the JSON's `runs` array is either:

- a **legacy flat entry** — a single boot's fields directly at the top
  level (`timestamp`, `metrics`, etc.), from before multi-boot capture
  existed, or
- a **multi-boot entry** (current format, produced by `capture`):
  ```json
  {
    "timestamp": "...",
    "build_path": "...",
    "boots": [ <default boot>, <debug boot> ]
  }
  ```

As of this version, a freshly captured multi-boot entry's `boots` list
always has exactly **2** items — the first default-phase boot and the
first debug-phase boot — even if `--num-boots` captured more per phase.
Older entries already on disk (written before this change) may still have
`2N` boots; they render as repeated columns and are left as-is — no
migration is performed.

Each boot object has:

- `timestamp` — `YYYY-MM-DD HH:MM`
- `build_path` — the nightly build path used
- `phase` — `"default"` or `"debug"` (absent on legacy entries)
- `log_name` — e.g. `"default-1"`, `"debug-1"` (absent on legacy entries;
  used to derive the simplified `Default`/`Debug` column header)
- `metrics` — object, fixed keys: `nhlos`, `kernel`, `initramfs`,
  `sysinit_svc`, `total_sysinit`, `total_multiuser`. Each metric has
  `value`, `note`, and optionally `hitters` (see below).
- `critical_chain` — ordered list of unit names (optional)
- `overall_overheads` — list of up to 3 hitter dicts (new, see below)
- `optimization_possibilities` — `null`, or a placeholder string (new, see below)
- `source` — free-text provenance string (optional)

`nhlos`, `initramfs`, and `total_sysinit` never carry `hitters` — there's
no per-component timing source for them today. `kernel` only carries
`hitters` on a debug-phase boot whose `dmesg` actually had
`initcall_debug` timing output (see below); it's absent otherwise,
including on every default-phase boot.

## Kernel-row debug hitters (new)

On a debug-phase boot, if that boot's `dmesg.txt` contains
`initcall_debug`-style timing/failure lines, `metrics.kernel.hitters` is
populated with up to 6 entries:

1. Up to 3 entries: the slowest probes/initcalls by usecs, descending.
   `{"name": "<probe/initcall>", "time": "<seconds> s", "note": "slowest probe/initcall"}`
2. Up to 3 entries: probe/initcall failures (deduped by name).
   `{"name": "<probe/initcall>", "time": "rc=<n>", "note": "probe/initcall failure"}` or
   `{"name": "<driver/device>", "time": "error <n>", "note": "probe failure"}`

If `dmesg` carries no such output at all (Type #2 bootloader, or any other
reason the debug cmdline params weren't active for that boot),
`metrics.kernel` has **no** `hitters` key — the same shape as a
default-phase boot. This absence is exactly what drives the rendered "No
debug prints enabled" subtitle under that boot's column header — it's a
content check, not a separately stored flag, so it's correct even for
runs produced via `--resume-pull` (which never touches the cmdline-editing
step at all).

## Overall Suspected Overheads (new)

`overall_overheads` is computed for **every** boot (default and debug
alike). It pools the *individual* hitters already present in that boot's
`metrics.kernel.hitters` (debug boots only), `metrics.sysinit_svc.hitters`,
and `metrics.total_multiuser.hitters`, filters out any hitter whose `time`
isn't a plain parseable duration (this excludes the Kernel row's
failure-kind entries, e.g. `"rc=-19"` or `"error -5"`, which don't compete
in a duration ranking), sorts the remaining pool by actual seconds
descending, and keeps the top 3.

This is **not** a comparison of stage totals (NHLOS vs. kernel vs.
initramfs vs. sysinit vs. userspace) — it's a re-ranking of the specific
units already flagged as high-hitters *within* those stages, so the top 3
are whichever individual costs (a specific slow probe, a specific slow
service, ...) are the biggest, regardless of which stage they happened to
occur in. On a default-phase boot (no kernel hitters to contribute), it's
still populated from that boot's sysinit + multi-user hitters.

## Optimization Possibilities (new)

`optimization_possibilities` is `null` by default. `bootbench.py` sets it
to a fixed placeholder string on a debug-phase boot whose `metrics.kernel`
has a non-empty `hitters` list (i.e., debug prints were actually active):

> "Not yet analyzed — ask an agent to review the overheads above and suggest fixes."

`bootbench.py` itself performs no analysis — this field is the one part of
the schema an agent is expected to hand-edit. Per the skill's Workflow
(see `SKILL.md`), after a capture completes an agent must read that boot's
`overall_overheads` (and, on the Debug boot, the Kernel row's
probe/initcall hitters), write concrete optimization suggestions naming
the actual unit/probe involved, replace the placeholder (or `null`) in
this field with that text, then re-render the HTML
(`report --report-cmd render`) so the report shows it. This is a sanctioned
exception to the "never hand-edit the JSON" rule — no other field should
be touched by hand. Default-phase boots, and debug-phase boots with no
active debug prints, keep it `null` (renders as `–`) unless there's a real
finding worth writing up from their own `overall_overheads`.

## Rank, not identity

Every `hitters` list (Kernel, sysinit, multi-user, and the pooled
`overall_overheads`) is ranked by time for that specific boot — it is not
a stable identity list. The same unit can appear at a different rank, or
not appear at all, on a different boot. This is expected, not a bug.

## Legacy flat hitters

Entries captured before hitters were merged into the metrics table may
carry a flat top-level `hitters` list instead of a nested
`metrics.total_multiuser.hitters`. Rendering treats a flat list as
belonging to `total_multiuser`, for backward compatibility.

## Retention

- JSON/HTML keep only the most recent 30 run entries (trimmed from the
  front on each `add_run`).
- Full history survives separately as per-boot `.txt` backups under
  `Boot-Logs/<target>/<build-folder>/`, unbounded, one per pulled boot
  (all `2N` of them, not just the 2 shown in the JSON/HTML). Numeric
  suffixes (`_2`, `_3`, ...) avoid overwriting when multiple boots finish
  within the same minute-granularity timestamp.

## Manually appending a run

Use `report --report-cmd add-run --run-json <path or ->`. This validates
the entry against the required schema (raises on missing keys) before
appending and triggering a re-render. Never bypass this by editing the
JSON file directly.
