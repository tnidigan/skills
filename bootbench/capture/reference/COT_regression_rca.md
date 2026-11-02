# COT: Root-Cause Analysis for `overall_overheads_text` (subsection (v))

## Goal

Produce a root-cause writeup for each finding already listed in
`overall_overheads_text` subsections (i)-(iii) (outliers, intra-run
regressions, previous-build regressions), grounded **only** in
bootbench's own telemetry — the pulled boot logs under
`Boot-Logs/<target>/<build-folder>/Logs-<log_name>/`, plus the
`regression_analysis` dict's own fields. This feeds subsection (v) of
`overall_overheads_text` (see `COT_outlier_detection.md` for the full
5-subsection structure this slots into).

This doc does **not** produce fixes. It produces an evidence-backed
statement of what most likely happened. Fix strategy is a separate,
later step — see `COT_optimization_recommendations.md`.

## Boot-log analysis method (apply this to every flagged finding)

Before reaching for the decision tree below, walk every finding through
this sequence — skipping steps is how confident-sounding wrong answers
happen:

1. **Boot overview first.** Open the boot's `critical_chain.txt`,
   `blame.txt` (or `blame_sysinit.txt`/`blame_multiuser.txt` if split),
   and `journalctl.log` together before drilling into one unit — a
   service's duration only means something in context of what else was
   running/blocking at the same time.
2. **Classify the time, don't just quote it.** For every unit under
   investigation, determine whether its reported duration is
   **execution time** (the unit itself was busy), **dependency-wait**
   (blocked on an `After=`/`Requires=` edge — check `critical_chain.txt`
   for what's indented above/below it), **failure time** (it errored out
   quickly but something else — a timeout or retry loop — padded the
   total), **timeout** (hit a `TimeoutStartSec`/`TimeoutStopSec` and was
   killed), or **restart time** (systemd restarted it — check for
   multiple start lines for the same unit in `journalctl.log`). These
   need different explanations and different fixes; don't collapse them.
3. **Failed/divergent units → `journalctl.log`.** For anything with
   `status: "failed"` or a `status_divergence` finding, grep
   `journalctl.log` (in the failing boot) around the unit's name for:
   PID, exit code / signal, explicit timeout messages, restart-loop
   evidence, a dependency/ordering failure, a permission error, a
   missing-file or missing-device error. Quote what you actually find —
   don't guess at a category without a matching log line.
4. **Correlate with kernel-level activity.** Check `dmesg.txt` (or the
   boot's kernel log equivalent) for anything co-occurring with the
   failure window: OOM kills, driver probe errors, firmware load
   failures, storage/USB/NAND errors. For kernel-probe/initcall findings
   specifically, also check `lsmod`/module-list output and
   `kernel_cmdline.txt`/`proc_config.txt` (debug boots only) for whether
   a relevant driver is built-in vs module, or a kernel config/cmdline
   option plausibly explains deferred/slow probing.
5. **Trace dependency edges.** For `critical_chain` source findings,
   read the actual chain (indentation = dependency depth) to see what
   the flagged unit was waiting on, and whether that upstream edge is the
   same across sibling boots / the previous build, or newly inserted.
6. **Only then** move to the decision tree below to pick the most likely
   category and confidence level.
7. **Report findings, not fixes.** Every writeup ends with: symptom,
   affected unit, duration-or-failure, evidence (exact log lines/files
   referenced), category, confidence, and next steps if confidence is
   not high. Never propose a definitive code/config fix here, and never
   directly edit a service file as "proof" of a cause — if you want to
   confirm a dependency-wait theory, say what capture you'd re-run, don't
   go modify unit files to test it.

## Decision tree, keyed by `source`

### `critical_chain`

Check whether the unit's position/duration in `critical_chain.txt`
changed vs. (a) its sibling boots in this same phase, or (b) the same
boot-phase in the previous build. 

- If a new dependency edge appears (unit now waits on something it
  didn't before) → **category: dependency-wait**, likely cause "an
  ordering change inserted a new wait," confidence medium-high if the
  new edge is visible in both this build's chain and absent from the
  previous build's chain for the same phase.
- If the edge is unchanged but the unit's own segment of the chain grew
  → treat as `sysinit_svc`/`blame`-style (see below) — the wait structure
  isn't the cause, the unit's own execution time is.

### `sysinit_svc` / `blame`

- If the SAME service also shows up regressed/outlier in another
  boot/phase of this same run (e.g. slow in both default-cmdline and
  debug-cmdline) → **category: execution-time**, cause is "intrinsic to
  the service, not boot-mode-specific," confidence high.
- If the service is slow ONLY in this one boot and its sibling boots are
  normal → check `journalctl.log` for this boot specifically for
  anything abnormal around its start time (resource contention, a
  concurrent unit also starting) before concluding anything; if nothing
  found, **category: ambiguous/isolated**, confidence low, next step
  "re-run capture to see if it reproduces."

### `kernel` (debug-cmdline boots only — probe/initcall timing or failure)

- If the SAME probe is also slow/failing across other debug-cmdline
  boots (sibling or previous build) → **category: kernel-probe,
  systemic**, confidence high — name the probe and the consistent
  pattern.
- If it's slow/failing in exactly one debug boot and clean in its
  siblings → **category: kernel-probe, transient/isolated**, confidence
  low-medium; check `dmesg.txt` for that boot for a concrete co-occurring
  error (EPROBE_DEFER, firmware load failure, timeout) before calling it
  purely transient — if you find one, cite it and raise confidence; if
  not, say explicitly "no corroborating dmesg signal, likely transient."
- If no debug-cmdline data exists at all for this build (default-only
  capture) → **category: kernel-probe, no-debug-data-available** — say so
  explicitly, recommend a debug-cmdline capture as the next step, don't
  guess.

### `failed_unit` (systemd reports `ACTIVE=failed`)

This is the systemd-level counterpart to the `kernel` source's probe
failures — same investigative instinct, journalctl-first:

- Grep `journalctl.log` (the boot where it failed) for the unit's name;
  look for the specific failure signature: non-zero exit code, a killing
  signal, `start-limit-hit` (restart loop exhausted retries), an explicit
  `Timed out waiting for...` line, a `Failed to...: Permission denied` /
  `No such file or directory` / `No such device` line, or an upstream
  dependency reported as `Dependency failed for ...`.
- If an exit code/signal/explicit error line is found → **category:
  failure, direct cause identified**, confidence high — quote the exact
  journalctl line.
- If the failure is a `Dependency failed` line → trace the named upstream
  unit; the real finding is on THAT unit, not this one — follow its
  `source` through this same decision tree, and say explicitly "this is a
  downstream symptom of `<upstream unit>`'s failure."
- If `dmesg.txt` for the same boot shows a device/firmware/storage error
  around the same timestamp (EPROBE_DEFER, missing firmware, I/O error,
  USB/NAND error) → correlate explicitly, **category: failure, hardware/
  driver-level**, confidence medium-high depending on how tight the time
  correlation is.
- If this unit is also the subject of a `status_divergence` outlier
  finding (failed in only some sibling boots) → explicitly state whether
  the journalctl signature differs between the failing boot(s) and
  whether anything distinguishes them from the succeeding boot(s) (e.g.
  a timing race, a resource that's sometimes not ready yet). If nothing
  distinguishes them → **category: ambiguous, intermittent**, confidence
  low, next step "re-run capture — insufficient evidence to explain why
  it's intermittent."
- If no journalctl/dmesg signal is found at all despite checking →
  explicitly say "journalctl/dmesg show no corroborating signal for this
  failure" rather than inventing a cause — this is itself a valid,
  reportable finding (it tells the reader more investigation outside
  bootbench's visibility is needed).

### No matching named unit at all (gap between kernel start and first systemd unit, or similar)

If a regression/outlier shows up as elapsed time with no corresponding
named unit in any of `critical_chain`/`blame`/`kernel` sources (e.g. a
gap in the overall boot timeline that doesn't map to a tracked unit) →
**category: firmware/NHLOS/bootloader-level**, outside bootbench's
visibility by design — state this plainly, recommend checking the
bootloader/firmware image diff between builds as the next step, don't
speculate about what's happening inside it.

### Ambiguous fallback

If none of the above produces a confident category — evidence is genuinely
inconclusive — say so explicitly: "No corroborating signal in
critical_chain/journalctl/dmesg to determine a likely cause." Recommend a
fresh debug-cmdline capture (if not already debug) or a repeat capture (if
already debug) as the next step. Do not force a category onto weak
evidence just to have an answer.

## Tone

Every line in subsection (v) must make it obvious what's an observed fact
(a quoted log line, a duration number, a chain position) vs. your
inference (the category and confidence you assigned from that evidence).
Use phrasing like "`journalctl.log` shows `<quoted line>`, which suggests
..." rather than asserting the category as if it were itself observed.
When confidence is low, say so plainly instead of hedging it away with
vague language — a clear "low confidence, here's why, here's the next
step" is more useful than a vague maybe.
