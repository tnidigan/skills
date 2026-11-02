# COT: Writing `overall_overheads_text` from `regression_analysis`

## Goal

Turn the raw, English-free findings in a boot's `regression_analysis`
dict (produced by `regression_analysis.build_analysis()`, see that
module's docstring) into the boot's `overall_overheads_text` field —
the prose rendered into the "Overall Suspected Overheads" report row.

This field is **100% agent-authored**. Python never writes prose into it
beyond the neutral "Not yet analyzed" placeholder `capture.py` sets at
capture time. You are reading this because you've been asked to replace
that placeholder with real analysis.

Unlike earlier drafts of this doc, **root-cause reasoning now lives in
this same field** (subsection (v) below), not in `optimization_possibilities`
— that field is reserved for fix/strategy recommendations only (see
`COT_optimization_recommendations.md`).

## Inputs Needed

- The boot's `regression_analysis` dict: `outliers`, `intra_run_regressions`,
  `previous_build_regressions`, `high_hitters`, `unit_appeared_once`.
- The boot's own `log_name` (e.g. `"default-1"` or `"debug-1"`) — only
  entries for THIS boot matter for subsections (i)-(iii) when a finding
  carries a `"boot"` key that doesn't match, it belongs to a sibling boot
  in the same phase and should be mentioned as cross-boot context, not
  attributed to this boot directly. `high_hitters` (iv) is already keyed
  by `log_name`, so look up `regression_analysis["high_hitters"][log_name]`
  specifically.

## Required structure

Write exactly **five subsections**, in this order, every time — even when
a subsection has nothing to report, write it with an explicit "none
observed" / "not available" line rather than omitting it. A reader must be
able to tell "this subsection was checked and came back clean" from "this
subsection was never written."

### (i) Outliers

Source: `regression_analysis["outliers"]`, filtered to this boot's phase
(outliers are detected per-phase across all 3 sibling boots, not just the
displayed one — mention a sibling-boot outlier too if it's informative,
but label it clearly as a different boot). Entries come in two kinds,
distinguished by the `"kind"` field:

**`kind: "duration"`** — a unit's duration in one boot is a statistical
outlier vs. its sibling boots. Format per finding:
> `<name>` (source: `<source>`) was a statistical outlier in boot `<boot>`:
> `<duration>`s vs. a sibling-boot median of `<sibling_median>`s
> (`<deviation_mads>` MAD above/below median).

Example (the canonical case this exists for): a service takes ~1s in two
sibling boots and 10s in the third — the 10s boot is flagged here.

**`kind: "status_divergence"`** — a unit (systemd unit reported `failed`
by `systemctl`, or a kernel probe/initcall failure on a debug boot)
**succeeded in some sibling boots and failed in others**. This is an
outlier regardless of duration — a pass/fail split across otherwise
identical boots is itself the anomaly. Format per finding:
> `<name>` (source: `<source>`) failed in boot(s) `<failed_in>` but
> succeeded in sibling boot(s) `<ok_in>` — flagged as an outlier (pass/fail
> divergence).

A unit that fails in **every** sibling boot of the phase is NOT a
divergence (there's no passing sibling to contrast against within this
run) — it will not appear here; check `high_hitters`' `failures` list (iv)
and `previous_build_regressions`-style reasoning in (v) instead.

If `outliers` has none for this phase: "No statistical outliers detected
across this phase's 3 boots (duration or pass/fail)."

**Caveat to always include when writing this subsection**: N=3 is a weak
sample for MAD-based detection. Phrase every `kind: "duration"` finding as
"a possible outlier" or "flagged as an outlier," never "confirmed" — 3
sibling boots isn't enough data to rule out a one-off scheduling hiccup.
`kind: "status_divergence"` findings don't need this hedge — a binary
pass/fail split is not a statistical judgment call.

### (ii) Regressions (this build, across boots)

Source: `regression_analysis["intra_run_regressions"]`, same phase.

Format per finding, using the user-facing terse numbered style:
> Regressions: 1) `<name>` — +`<delta>`s (`<boot>`) vs `<sibling_baseline>`s
> (sibling baseline, `<delta_pct>`%)

If none: "No intra-run regressions detected — all named units were
consistent within +15% across this phase's 3 boots."

### (iii) Regressions vs previous build

Source: `regression_analysis["previous_build_regressions"]`.

Format per finding, matching the user's required display format exactly:
> Regressions: 1) `<name>` — +`<delta>`s (`<boot>`) than `<previous_duration>`s
> (`<previous phase's boot-1>` of prev build: `<previous_build_path>`)

If the list is empty **because there is no previous build yet** (i.e. this
is the first captured run for this device), write exactly: "No previous
build available for comparison." Never silently omit this subsection, and
never confuse "no previous build" with "no regressions found" — if you
can tell which case applies (check whether any history run exists), say
so explicitly.

### (iv) High hitters in this boot

Source: `regression_analysis["high_hitters"][this_boot's_log_name]` — a
dict with two parallel lists, independent of whether anything above
flagged it:

- `ranked` — the biggest-duration named units, by raw magnitude. Format,
  one line per entry:
  > #`<rank>`. `<name>` (source: `<source>`) — `<duration>`s

  List every entry Python returned (up to `HIGH_HITTER_TOP_N`); don't
  further trim it yourself — Python already capped it generously because
  ranking is its job, but judging relevance for the reader is yours. You
  may add a one-line comment after an entry if it's also flagged in
  (i)/(ii)/(iii) above (e.g. "— also flagged as an outlier, see above"),
  but don't repeat the full finding.
- `failures` — this boot's own failed units/probes (no duration to rank
  by — includes units that failed consistently across all 3 sibling
  boots, which (i) deliberately excludes). Format:
  > Failures observed: `<name>` (source: `<source>`), ...

  If `failures` is empty: "No failed units/probes observed in this boot."

### (v) Root Cause Analysis

For **every** finding written up in (i), (ii), and (iii) above, apply
`COT_regression_rca.md`'s decision tree and write the full RCA structure
for that finding, right here in this same field:

> **Finding**: `<name>` (source `<source>`, boot `<boot>`)
> **Symptom**: `<what was observed — outlier/regression/failure, with numbers>`
> **Evidence**: `<which raw log file(s) and what they show>`
> **Most likely category**: `<execution-time | dependency-wait | failure | timeout | restart | kernel-probe | firmware/NHLOS | ambiguous>`
> **Confidence**: `<high | medium | low>`, because `<why>`
> **Recommended next step**: `<what to check next if confidence isn't high>`

Follow `COT_regression_rca.md` in full for how to fill in each line — it
is not optional reading, it is the actual methodology for this
subsection. Do not skip straight to a cause without first walking through
the evidence for each specific finding; a reader must be able to tell
observed fact from your inference. Never claim a root cause with no
supporting evidence, never propose a definitive fix here (that's
`optimization_possibilities`'s job, and even there only a strategy, not a
guaranteed fix), and say explicitly when more logs or a repeat capture are
needed before you can go further.

## `unit_appeared_once` — investigate it, don't dead-end on "insufficient data"

`regression_analysis["unit_appeared_once"]` lists units that showed up in
only one of a phase's 3 boots (e.g. a one-shot service that happened to run
in boot 2 but not boots 1/3). There's no sibling-duration data to compare
against, so these are statistically NOT outliers or regressions — do not
list them under (i) or (ii), and never claim a single-sample finding as a
statistical outlier.

**But "no sibling duration to compare" is not the same as "nothing to
say."** Each entry carries `missing_from` (the sibling boots the unit is
absent from) and `raw_log_hint` (which pulled raw log file to check, under
`Boot-Logs/<target>/<build-folder>/Logs-<log_name>/`, and that these raw
per-boot logs are kept permanently — they're never trimmed, unlike
`bootchart-data-<slug>.json`'s `MAX_RUNS`). Before writing anything off,
actually open the hinted file(s) for the missing sibling boot(s) (and that
boot's `journalctl.log`) and look for why the unit is absent there — e.g.:

- The unit genuinely only runs conditionally (a one-shot migration/update
  service, a udev-triggered unit for hardware not present every boot) —
  say so, naming what you found in the log that supports it.
- The unit failed silently or was masked in the other boots — grep
  `journalctl.log` for the unit's name in each missing boot; if it shows a
  failure/skip line there, name that explicitly rather than just noting
  the absence.
- The unit genuinely never started in those boots and the raw logs show no
  trace at all — say that explicitly too; "the raw logs show no sign of
  this unit in default-2/default-3" is a real finding, not a cop-out,
  as long as you actually looked.

Report what you found under (iv) (or as a short aside if it's also a high
hitter), phrased as e.g. "`<name>` appeared only in `<boot>`; checked
`<missing sibling boots>`'s raw logs — `<what you found>`." Only fall back
to a generic "insufficient data to compare, raw logs show no further
signal" if you actually checked the hinted log(s) and found nothing more
to go on — never use that phrase as a default without having looked.

## Tone

Keep (i)-(iv) terse and factual — name, boot, numbers. (v) is where
interpretation happens — be explicit about what's observed fact vs. your
inference. Fix recommendations belong in `optimization_possibilities`; see
`COT_optimization_recommendations.md` for that half.
