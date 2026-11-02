# COT: Optimization Recommendations for `optimization_possibilities`

## Goal

Once `COT_regression_rca.md` has produced a root cause for a flagged unit
(written into `overall_overheads_text` subsection (v) — see
`COT_outlier_detection.md`), recommend a concrete fix strategy. This is
the entire content of the boot's separate `optimization_possibilities`
field — fix/strategy text only, no re-statement of the analysis itself.

This doc is a **starting knowledge base of known boot-time fix patterns**,
not an exhaustive catalogue — if a flagged unit's cause doesn't match any
pattern below, say so and recommend investigation rather than forcing a
mismatched suggestion onto it.

## Inputs Needed

- The root cause already established via `COT_regression_rca.md` and
  written into `overall_overheads_text` subsection (v).
- The flagged unit's `source` (`critical_chain`, `sysinit_svc`, `blame`,
  `kernel`, or `failed_unit`) — most patterns below are source-specific.

## Known fix patterns, by cause

### A slow `.service` is on the critical path, but doesn't need to be

If a `sysinit_svc`/`blame` service is both slow AND sits in
`critical_chain` ahead of unrelated units: recommend investigating whether
it can be **deferred** (`After=`/`Wants=` moved later, or switched from
`sysinit.target` to a later target) or **parallelized** (removing an
unnecessary `Before=`/`After=` ordering dependency that's serializing it
with something it doesn't actually need to wait for).

### A service grew in duration, cause unknown but reproducible

If the RCA step found the service itself growing run-over-run (not caused
by reordering), recommend profiling that specific service's own startup
(e.g. `systemd-analyze blame`-equivalent already captured in
`total_multiuser`/`sysinit_svc` hitters — check if ITS OWN dependencies or
work changed, e.g. a config file it reads growing, a network call it makes
newly blocking).

### An unused or non-essential unit is costing boot time

If a flagged unit is a `.service`/`.socket`/`.device` not actually needed
for this device's deployed use case, recommend **disabling or masking** it
(`systemctl disable`/`mask`) rather than optimizing it — the cheapest fix
is removing work, not speeding it up.

### A long `critical_chain` dependency

If (i)/(ii) flagged a `critical_chain`-sourced reordering, recommend
reviewing whether the new/moved dependency edge is actually required
(`systemd-analyze critical-chain`-equivalent already available as the
captured `critical_chain` list) — an overly conservative `After=`/
`Requires=` edge added for safety during development sometimes outlives
its need.

### A kernel probe/initcall is slow (debug-boot `source: kernel` findings)

If RCA identified a systemic slow probe: recommend checking whether that
driver supports **deferred probing** (`-EPROBE_DEFER`) so it doesn't block
other initcalls, or whether the probe is doing synchronous work (e.g. a
blocking regulator/clock wait) that could become asynchronous.

If RCA identified a transient (isolated-to-one-boot) slow probe instead:
do NOT recommend a code fix — recommend re-running the debug-cmdline
capture to confirm reproducibility before investing engineering time, since
a one-off scheduling/race delay isn't a build regression.

### A unit failed in exactly one sibling boot (`status_divergence`, source `failed_unit` or `kernel`)

If RCA could not find anything distinguishing the failing boot from its
succeeding siblings (genuinely intermittent): do NOT recommend a fix —
recommend re-running capture to confirm reproducibility before investing
engineering time, the same way a transient kernel-probe finding is
handled above. A single intermittent failure out of 3 boots isn't yet a
build regression.

If RCA DID find a distinguishing signal (e.g. a timing race against
another unit, a resource not always ready) — recommend addressing that
specific race: typically an explicit `After=`/ordering dependency on the
resource it's racing against, or (if the unit itself supports it) a
retry/wait-for-resource option, rather than a generic "flaky" dismissal.

### A unit fails consistently across all sibling boots (not a divergence — surfaced via High Hitters' `failures` list)

Do not recommend a fix until the RCA step has produced an actual cause
from `journalctl.log`/`dmesg.txt` evidence (exit code, signal, dependency
failure, device/firmware error) — a unit failing 3/3 times is important
enough to flag at the top of this field as needing attention, but
recommending a specific fix (restart policy change, dependency fix,
disabling the unit) without a confirmed cause first risks fixing the
wrong thing. If RCA found a clear cause, recommend the fix that matches
that cause using the patterns above (e.g. dependency fix if it's a
`Dependency failed` chain, firmware investigation if correlated with a
`dmesg.txt` hardware error).

### NHLOS/firmware/bootloader-level finding

If RCA attributed the slowdown to NHLOS/firmware (no named-unit telemetry
available), recommend checking the build's firmware/bootloader image diff
against the previous build directly — this is outside bootbench's and
systemd's visibility, so the fix (if any) lives in the firmware build, not
in anything this report can point at more specifically.

### Ambiguous root cause

If RCA could not establish a cause, do not recommend a fix — recommend the
same next step RCA already suggested (a fresh debug-cmdline capture) and
stop there. Recommending a fix without a cause is guessing, not analysis.

## Scope note

This list reflects boot-time patterns common to systemd-based Linux
targets and Qualcomm initcall/probe timing; it is deliberately not
exhaustive. If you identify a new recurring fix pattern while doing this
analysis across multiple runs, consider adding it here so future analysis
benefits from it too.
