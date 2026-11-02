#!/usr/bin/env python3
r"""
regression_analysis.py -- cross-boot outlier + regression detection for a
single `capture` run's 6 boots (3 default-cmdline + 3 debug-cmdline), plus
regression detection against the immediately previous build in this
device's history.

Pure-function module: no device I/O, no file I/O (besides its own
--selftest), and -- deliberately -- no English text output. It only ever
returns structured findings, each naming a specific unit (a service /
device / probe / socket pulled from `critical_chain`, `sysinit_svc`
hitters, `blame` hitters, (debug boots only) `kernel` hitters, or
`failed_unit`/`kernel`-probe failure signal), the boot it was observed in,
and its duration (where it has one). Turning that raw data into the prose
written into the report's "Overall Suspected Overheads" /
"Optimization Possibilities" cells is entirely the agent's job, guided by
the COT docs in `capture/reference/` -- this module does not rank, pool, or
caption anything for display, unlike the hardcoded `overall_overheads`
logic it replaces in `capture.build_run()`.

Five finding categories, orchestrated by build_analysis():
  (i)   outliers                  -- two different kinds, both tagged via
                                      a "kind" field so consumers can branch:
                                      - kind="duration": a unit's duration in
                                        one boot is a statistical outlier vs.
                                        its own phase's sibling boots
                                        (MAD-based, N=3 per phase)
                                      - kind="status_divergence": a unit
                                        (systemd unit reported failed by
                                        `systemctl`, or a kernel probe/initcall
                                        failure on a debug boot) failed in
                                        some of a phase's sibling boots but
                                        not others -- a unit failing
                                        CONSISTENTLY across all sibling boots
                                        is NOT a divergence (nothing to
                                        compare against within this run) and
                                        is surfaced instead via high_hitters'
                                        "failures" list below.
  (ii)  intra_run_regressions     -- a unit runs meaningfully slower in one
                                      boot than in its own phase's other boots,
                                      within this same capture run
  (iii) previous_build_regressions -- a unit in this run's displayed boot-1
                                      (default or debug) runs meaningfully
                                      slower than the SAME unit in the
                                      immediately preceding saved run's
                                      same-phase boot-1. Comparing directly
                                      against history_runs[-1] (whatever was
                                      most recently saved) means this run
                                      automatically becomes "the previous
                                      build" for the NEXT run's comparison --
                                      no separate bookkeeping needed.
  (iv)  high_hitters              -- {"ranked": [...], "failures": [...]}
                                      per displayed boot. "ranked" is the
                                      biggest-duration named units by raw
                                      magnitude, no statistical filtering at
                                      all. "failures" is this boot's own
                                      failed systemd units / kernel probe
                                      failures (no duration to rank by, but
                                      still worth listing) -- the agent
                                      decides what's worth writing up from
                                      either list.

A fifth, informational-only list, `unit_appeared_once`, covers units with no
sibling-boot data to compare against (present in only 1 of a phase's 3
boots) -- these are deliberately NOT classified as outliers/regressions
(insufficient sample), but each entry still carries `missing_from` (the
sibling boots it's absent from) and `raw_log_hint` (which pulled raw log
file, under Boot-Logs/<target>/<build-folder>/Logs-<log_name>/, to check for
why) so the agent can actually investigate instead of writing the finding
off with no further action.

Run standalone to self-check the detection logic against synthetic data
(no device, no real run data needed):
    py -3 regression_analysis.py --selftest
"""

from __future__ import annotations

import re
import sys

MAD_K = 2.5
# benchmarks' regression-detection-and-rca skill uses 3.0 for its larger
# sample sizes; bootbench only ever has 3 sibling boots per phase, so a
# stricter multiplier would almost never trip -- 2.5 is loosened
# deliberately and documented as tunable once real hardware variance data
# exists across many captured runs.

INTRA_RUN_REGRESSION_PCT = 15.0
# benchmarks' throughput regression thresholds (3-7%) assume far less
# run-to-run noise than boot time on embedded hardware; this is
# intentionally looser. Reused as-is for the previous-build comparison too
# (kind iii) -- a single prior data point deserves at least as much slack
# as a 3-sample intra-run comparison, not less.

HIGH_HITTER_TOP_N = 8
# Generous on purpose: Python's job is only to rank, not to decide what's
# worth mentioning -- that judgment call belongs to the agent, per
# COT_outlier_detection.md. Also caps high_hitters' "failures" list.


def _median(values):
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def _parse_seconds(time_str):
    if not time_str:
        return None
    m = re.search(r"[-+]?\d+(?:\.\d+)?", time_str)
    return float(m.group()) if m else None


FAILURE_NOTES = ("probe/initcall failure", "probe failure")
# capture.parse_kernel_hitters tags a kernel-probe-failure hitter with one of
# these two "note" values and a non-numeric "time" (e.g. "rc=-12", "error -12")
# -- never a real duration. Both collect_named_hitters (duration pipeline)
# and _boot_failed_names (failure pipeline) key off this same tuple.


def collect_named_hitters(boot: dict) -> list:
    """Flattens a boot's already-parsed named-entry DURATION sources into one
    list -- critical_chain/sysinit_svc/blame/kernel hitters that have a real,
    comparable duration. Kernel probe/initcall FAILURES are deliberately
    excluded here (they have no duration to compare) -- see
    _boot_failed_names for those. Reuses the fields capture.build_run()
    already populates verbatim -- no re-parsing of raw log text happens
    here."""
    named = []
    for name in boot.get("critical_chain") or []:
        named.append({"name": name, "duration": None, "source": "critical_chain"})
    # critical_chain entries carry no per-unit duration of their own (it's a
    # dependency-order list, not a cost ranking -- see capture.parse_critical_chain);
    # only sysinit_svc/kernel/blame hitters carry a "time" string to parse into seconds.
    for h in (boot.get("metrics", {}).get("sysinit_svc", {}) or {}).get("hitters") or []:
        named.append({"name": h["name"], "duration": _parse_seconds(h.get("time")), "source": "sysinit_svc"})
    for h in (boot.get("metrics", {}).get("kernel", {}) or {}).get("hitters") or []:
        if h.get("note") in FAILURE_NOTES:
            continue  # time is "rc=<n>"/"error <n>", not a duration -- _parse_seconds
                      # would otherwise misread e.g. "rc=-12" as -12.0 seconds
        secs = _parse_seconds(h.get("time"))
        if secs is None:
            continue
        named.append({"name": h["name"], "duration": secs, "source": "kernel"})
    for h in (boot.get("metrics", {}).get("total_multiuser", {}) or {}).get("hitters") or []:
        named.append({"name": h["name"], "duration": _parse_seconds(h.get("time")), "source": "blame"})
    # Only entries with a real, comparable duration are usable for
    # outlier/regression math -- critical_chain's bare names (no "time")
    # are kept out of named/comparable sets but still available via
    # "unit_appeared_once"-style context if ever needed.
    return [n for n in named if n["duration"] is not None]


def _boot_failed_names(boot: dict) -> list:
    """This boot's own failed systemd units (from metrics.failed_units,
    parsed by capture.parse_failed_units out of `systemctl`'s dump) plus any
    kernel probe/initcall failures (debug boots only). Deduped by name,
    source tagged so the agent knows which raw log to check."""
    failed, seen = [], set()
    for u in (boot.get("metrics", {}).get("failed_units") or []):
        if u["name"] not in seen:
            seen.add(u["name"])
            failed.append({"name": u["name"], "source": "failed_unit"})
    for h in (boot.get("metrics", {}).get("kernel", {}) or {}).get("hitters") or []:
        if h.get("note") in FAILURE_NOTES and h["name"] not in seen:
            seen.add(h["name"])
            failed.append({"name": h["name"], "source": "kernel"})
    return failed


RAW_LOG_FILE_BY_SOURCE = {
    "sysinit_svc": "systemd-analyze_critical_chain_sysinit.txt",
    "blame": "blame_systemd.txt",
    "kernel": "dmesg.txt",
    "critical_chain": "systemd-analyze_critical_chain.txt",
    "failed_unit": "blame_systemdsystemctl.txt",
}


def _raw_log_hint(source, missing_from):
    """A unit appearing in only one sibling boot is NOT itself a finding --
    it's a pointer to go read raw per-boot logs (pulled via adb into
    Boot-Logs/<target>/<build-folder>/Logs-<log_name>/*.txt and never
    trimmed, unlike bootchart-data.json's MAX_RUNS) and work out WHY the
    unit is missing from the other boots (disabled conditionally, failed
    silently, present but renamed, etc.) rather than writing it off as
    'insufficient data to compare'."""
    filename = RAW_LOG_FILE_BY_SOURCE.get(source, "journalctl.log")
    missing_logs = ", ".join(f"Logs-{ln}/{filename}" for ln in missing_from)
    return f"Check {missing_logs} (and Logs-*/journalctl.log) for why this unit doesn't appear there."


def group_by_phase(all_boots: list) -> dict:
    by_phase = {}
    for boot in all_boots:
        by_phase.setdefault(boot["phase"], []).append(boot)
    return by_phase


def detect_status_divergence(phase_boots: list) -> list:
    """Outliers (i), status_divergence kind: a unit that `systemctl` (or, on
    debug boots, a kernel probe/initcall) reports as FAILED in some of this
    phase's sibling boots but not others. `metrics.failed_units` is an
    exhaustive per-boot list (unlike the duration hitters, which are only
    the top-N) -- so "name not in this boot's failed set" is read as "this
    boot's instance of the unit did not fail", not as "insufficient data".
    A unit failing in EVERY sibling boot is consistent, not divergent --
    excluded here, surfaced instead via get_high_hitters' "failures" list."""
    all_log_names = [b["log_name"] for b in phase_boots]
    failed_by_boot, source_by_name = {}, {}
    for b in phase_boots:
        names = set()
        for entry in _boot_failed_names(b):
            names.add(entry["name"])
            source_by_name.setdefault(entry["name"], entry["source"])
        failed_by_boot[b["log_name"]] = names

    all_failed_names = set()
    for names in failed_by_boot.values():
        all_failed_names |= names
    phase = phase_boots[0]["phase"] if phase_boots else None

    divergences = []
    for name in sorted(all_failed_names):
        failed_in = [ln for ln in all_log_names if name in failed_by_boot[ln]]
        ok_in = [ln for ln in all_log_names if name not in failed_by_boot[ln]]
        if failed_in and ok_in:
            divergences.append({
                "kind": "status_divergence", "name": name, "source": source_by_name[name],
                "phase": phase, "failed_in": failed_in, "ok_in": ok_in,
            })
    return divergences


def detect_unit_outliers_and_regressions(phase_boots: list) -> dict:
    """Outliers (i), kind="duration", and intra-run regressions (ii) for one
    phase's sibling boots (e.g. default-1/2/3), by named unit duration."""
    named_by_boot = {boot["log_name"]: collect_named_hitters(boot) for boot in phase_boots}
    phase = phase_boots[0]["phase"] if phase_boots else None
    all_log_names = list(named_by_boot.keys())

    keys = set()
    for named in named_by_boot.values():
        for n in named:
            keys.add((n["name"], n["source"]))

    outliers, intra_run, appeared_once = [], [], []
    for name, source in keys:
        values = {}
        for log_name, named in named_by_boot.items():
            for n in named:
                if n["name"] == name and n["source"] == source:
                    values[log_name] = n["duration"]
                    break

        if len(values) < 2:
            (log_name,) = values.keys()
            missing_from = [ln for ln in all_log_names if ln != log_name]
            appeared_once.append({
                "name": name, "source": source, "phase": phase,
                "boot": log_name, "duration": values[log_name],
                "missing_from": missing_from,
                "raw_log_hint": _raw_log_hint(source, missing_from),
            })
            continue

        vals = list(values.values())
        median = _median(vals)
        mad = _median([abs(v - median) for v in vals])

        for log_name, v in values.items():
            if mad > 0 and abs(v - median) > MAD_K * mad:
                outliers.append({
                    "kind": "duration", "name": name, "source": source, "phase": phase, "boot": log_name,
                    "duration": v, "sibling_median": median,
                    "deviation_mads": round(abs(v - median) / mad, 2),
                })

            others = [ov for ln, ov in values.items() if ln != log_name]
            baseline = _median(others)
            if baseline > 0:
                delta_pct = (v - baseline) / baseline * 100
                if delta_pct > INTRA_RUN_REGRESSION_PCT:
                    intra_run.append({
                        "name": name, "source": source, "phase": phase, "boot": log_name,
                        "duration": v, "sibling_baseline": baseline,
                        "delta_pct": round(delta_pct, 1),
                    })

    return {"outliers": outliers, "intra_run_regressions": intra_run, "unit_appeared_once": appeared_once}


def detect_previous_build_regressions(displayed_boots: list, history_runs: list) -> list:
    """Regressions (iii): this run's displayed boot-1 of each phase vs. the
    SAME phase's boot-1 in the immediately preceding saved run
    (history_runs[-1]). Returns [] if there is no previous build yet."""
    if not history_runs:
        return []

    previous_run = history_runs[-1]
    previous_boots = previous_run.get("boots") or [previous_run]

    findings = []
    for boot in displayed_boots:
        prev_boot = next((b for b in previous_boots if b.get("phase") == boot.get("phase")), None)
        if prev_boot is None:
            continue

        current_named = {(n["name"], n["source"]): n["duration"] for n in collect_named_hitters(boot)}
        previous_named = {(n["name"], n["source"]): n["duration"] for n in collect_named_hitters(prev_boot)}

        for key, cur_v in current_named.items():
            if key not in previous_named:
                continue  # a unit absent from the previous build isn't a regression, just new
            prev_v = previous_named[key]
            if prev_v <= 0:
                continue
            delta_pct = (cur_v - prev_v) / prev_v * 100
            if delta_pct > INTRA_RUN_REGRESSION_PCT:
                findings.append({
                    "name": key[0], "source": key[1], "phase": boot["phase"], "boot": boot["log_name"],
                    "current_duration": cur_v, "previous_duration": prev_v,
                    "delta_pct": round(delta_pct, 1),
                    "previous_build_path": previous_run.get("build_path"),
                })
    return findings


def get_high_hitters(boot: dict, top_n: int = HIGH_HITTER_TOP_N) -> dict:
    """High hitters (iv): {"ranked": [...], "failures": [...]} for this
    boot. "ranked" is the top_n biggest-duration named units by raw
    magnitude -- no statistical filtering. "failures" is this boot's own
    failed systemd units / kernel probe failures (no duration to rank by)."""
    named = collect_named_hitters(boot)
    named_sorted = sorted(named, key=lambda n: n["duration"], reverse=True)
    ranked = [{"name": n["name"], "source": n["source"], "duration": n["duration"]} for n in named_sorted[:top_n]]
    failures = _boot_failed_names(boot)[:top_n]
    return {"ranked": ranked, "failures": failures}


def build_analysis(all_boots: list, displayed_boots: list, history_runs: list) -> dict:
    """Orchestrates all five finding categories. Returns pure structured
    data -- no rendering, no English text, no hardcoded ranking/pooling."""
    by_phase = group_by_phase(all_boots)

    outliers, intra_run, appeared_once = [], [], []
    for phase_boots in by_phase.values():
        r = detect_unit_outliers_and_regressions(phase_boots)
        outliers += r["outliers"]
        intra_run += r["intra_run_regressions"]
        appeared_once += r["unit_appeared_once"]
        outliers += detect_status_divergence(phase_boots)

    return {
        "outliers": outliers,
        "intra_run_regressions": intra_run,
        "previous_build_regressions": detect_previous_build_regressions(displayed_boots, history_runs),
        "high_hitters": {b["log_name"]: get_high_hitters(b) for b in displayed_boots},
        "unit_appeared_once": appeared_once,
    }


# ---------------------------------------------------------------------------
# Self-check (synthetic data; no device, no pytest dependency -- consistent
# with the rest of bootbench, which has no test framework today)
# ---------------------------------------------------------------------------

def _make_boot(phase, n, sysinit_hitters=None, kernel_hitters=None, blame_hitters=None,
               critical_chain=None, failed_units=None):
    return {
        "phase": phase,
        "log_name": f"{phase}-{n}",
        "critical_chain": critical_chain or [],
        "metrics": {
            "sysinit_svc": {"hitters": sysinit_hitters or []},
            "kernel": {"hitters": kernel_hitters or []},
            "total_multiuser": {"hitters": blame_hitters or []},
            "failed_units": failed_units or [],
        },
    }


def _selftest():
    # 3 default boots: wpa_supplicant.service steady around 0.30-0.33s
    default_boots = [
        _make_boot("default", 1, sysinit_hitters=[{"name": "wpa_supplicant.service", "time": "0.31 s"}]),
        _make_boot("default", 2, sysinit_hitters=[{"name": "wpa_supplicant.service", "time": "0.33 s"}]),
        _make_boot("default", 3, sysinit_hitters=[{"name": "wpa_supplicant.service", "time": "0.30 s"}]),
    ]
    # 3 debug boots: qcom-probe-xyz is 3x slower in debug-3 -> outlier + intra-run regression
    debug_boots = [
        _make_boot("debug", 1, kernel_hitters=[{"name": "qcom-probe-xyz", "time": "0.200 s"}]),
        _make_boot("debug", 2, kernel_hitters=[{"name": "qcom-probe-xyz", "time": "0.210 s"}]),
        _make_boot("debug", 3, kernel_hitters=[{"name": "qcom-probe-xyz", "time": "0.650 s"}]),
    ]
    all_boots = default_boots + debug_boots
    displayed = [default_boots[0], debug_boots[0]]

    # No previous build yet.
    analysis_no_history = build_analysis(all_boots, displayed, history_runs=[])
    assert analysis_no_history["previous_build_regressions"] == [], "expected no previous-build regressions when history is empty"
    assert any(o["name"] == "qcom-probe-xyz" and o["boot"] == "debug-3" and o["kind"] == "duration" for o in analysis_no_history["outliers"]), \
        "expected qcom-probe-xyz in debug-3 to be flagged as a duration outlier"
    assert any(r["name"] == "qcom-probe-xyz" and r["boot"] == "debug-3" for r in analysis_no_history["intra_run_regressions"]), \
        "expected qcom-probe-xyz in debug-3 to be flagged as an intra-run regression"
    assert set(analysis_no_history["high_hitters"].keys()) == {"default-1", "debug-1"}, \
        "expected high_hitters keyed by both displayed boots' log_name"
    assert set(analysis_no_history["high_hitters"]["default-1"].keys()) == {"ranked", "failures"}, \
        "expected high_hitters[boot] to be a {ranked, failures} dict"

    # A unit present in only one boot of a phase should land in
    # unit_appeared_once, not outliers -- and should carry enough context
    # (missing_from, raw_log_hint) to actually go investigate the raw logs,
    # not just a dead-end "insufficient data" label.
    sparse_default_boots = [
        _make_boot("default", 1, sysinit_hitters=[{"name": "one-shot.service", "time": "0.05 s"}]),
        _make_boot("default", 2),
        _make_boot("default", 3),
    ]
    sparse_analysis = build_analysis(sparse_default_boots + debug_boots, [sparse_default_boots[0], debug_boots[0]], history_runs=[])
    assert any(u["name"] == "one-shot.service" for u in sparse_analysis["unit_appeared_once"]), \
        "expected one-shot.service to land in unit_appeared_once"
    assert not any(o.get("name") == "one-shot.service" for o in sparse_analysis["outliers"]), \
        "one-shot.service must not be flagged as an outlier (insufficient data)"
    one_shot = next(u for u in sparse_analysis["unit_appeared_once"] if u["name"] == "one-shot.service")
    assert set(one_shot["missing_from"]) == {"default-2", "default-3"}, \
        "expected one-shot.service's missing_from to name the two sibling boots it didn't appear in"
    assert "Logs-default-2" in one_shot["raw_log_hint"] and "Logs-default-3" in one_shot["raw_log_hint"], \
        "expected raw_log_hint to point at both missing sibling boots' pulled raw logs"

    # A kernel probe FAILURE entry ("time": "rc=-12") must never be
    # misread as a -12.0 second duration by _parse_seconds -- it should be
    # dropped from comparable named hitters entirely, not surfaced as a
    # bogus outlier/regression.
    probe_fail_boot = _make_boot(
        "debug", 9,
        kernel_hitters=[{"name": "qcom-weird-probe", "time": "rc=-12", "note": "probe/initcall failure"}],
    )
    assert collect_named_hitters(probe_fail_boot) == [], \
        "expected a probe-failure kernel hitter (time='rc=-12') to be excluded, not parsed as -12.0s"

    # Status divergence: a systemd unit fails (per `systemctl`) in exactly
    # one of a phase's 3 sibling boots, succeeds in the other two -> must be
    # flagged as an outlier (kind=status_divergence), per the user's explicit
    # ask that a pass/fail split across iterations counts as an outlier.
    divergent_boots = [
        _make_boot("default", 1),
        _make_boot("default", 2, failed_units=[{"name": "modem-helper.service", "sub_state": "failed"}]),
        _make_boot("default", 3),
    ]
    divergence_analysis = build_analysis(
        divergent_boots + debug_boots, [divergent_boots[0], debug_boots[0]], history_runs=[],
    )
    divergence_findings = [o for o in divergence_analysis["outliers"] if o.get("kind") == "status_divergence"]
    assert any(
        d["name"] == "modem-helper.service" and d["failed_in"] == ["default-2"]
        and set(d["ok_in"]) == {"default-1", "default-3"}
        for d in divergence_findings
    ), "expected modem-helper.service to be flagged as a status_divergence outlier (failed in default-2 only)"

    # A unit failing CONSISTENTLY across all 3 sibling boots is not a
    # divergence (nothing to compare against within this run) -- it must
    # NOT be flagged as an outlier, but must still show up in that boot's
    # high_hitters "failures" list.
    consistent_fail_boots = [
        _make_boot("default", 1, failed_units=[{"name": "always-broken.service", "sub_state": "failed"}]),
        _make_boot("default", 2, failed_units=[{"name": "always-broken.service", "sub_state": "failed"}]),
        _make_boot("default", 3, failed_units=[{"name": "always-broken.service", "sub_state": "failed"}]),
    ]
    consistent_fail_analysis = build_analysis(
        consistent_fail_boots + debug_boots, [consistent_fail_boots[0], debug_boots[0]], history_runs=[],
    )
    assert not any(
        o.get("name") == "always-broken.service" for o in consistent_fail_analysis["outliers"]
    ), "a unit failing in all 3 sibling boots must not be flagged as a status_divergence outlier"
    assert any(
        f["name"] == "always-broken.service" for f in consistent_fail_analysis["high_hitters"]["default-1"]["failures"]
    ), "expected always-broken.service to appear in default-1's high_hitters failures list"

    # Previous build exists, with wpa_supplicant.service noticeably slower this time.
    previous_run = {
        "build_path": r"\\swayam\nightly\prev_build\performance",
        "boots": [
            _make_boot("default", 1, sysinit_hitters=[{"name": "wpa_supplicant.service", "time": "0.20 s"}]),
            _make_boot("debug", 1, kernel_hitters=[{"name": "qcom-probe-xyz", "time": "0.190 s"}]),
        ],
    }
    analysis_with_history = build_analysis(all_boots, displayed, history_runs=[previous_run])
    prev_regressions = analysis_with_history["previous_build_regressions"]
    assert any(
        r["name"] == "wpa_supplicant.service" and r["boot"] == "default-1" and r["previous_build_path"] == previous_run["build_path"]
        for r in prev_regressions
    ), "expected wpa_supplicant.service in default-1 to be flagged as regressed vs the previous build"

    # Feeding this run's own JSON back in as "history" for a fresh current
    # run demonstrates it automatically becomes the next run's reference.
    this_run_as_history = {
        "build_path": r"\\swayam\nightly\this_build\performance",
        "boots": displayed,
    }
    next_default_boots = [
        _make_boot("default", 1, sysinit_hitters=[{"name": "wpa_supplicant.service", "time": "0.95 s"}]),
        _make_boot("default", 2, sysinit_hitters=[{"name": "wpa_supplicant.service", "time": "0.92 s"}]),
        _make_boot("default", 3, sysinit_hitters=[{"name": "wpa_supplicant.service", "time": "0.93 s"}]),
    ]
    next_debug_boots = [_make_boot("debug", i + 1) for i in range(3)]
    next_displayed = [next_default_boots[0], next_debug_boots[0]]
    next_analysis = build_analysis(
        next_default_boots + next_debug_boots, next_displayed, history_runs=[this_run_as_history],
    )
    assert any(
        r["name"] == "wpa_supplicant.service" and r["previous_build_path"] == this_run_as_history["build_path"]
        for r in next_analysis["previous_build_regressions"]
    ), "expected this run to automatically become the next run's previous-build reference"

    print("All regression_analysis self-checks passed.")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("Usage: py -3 regression_analysis.py --selftest")
        sys.exit(1)
