#!/usr/bin/env python3
r"""
report.py -- per-device JSON history + regenerated HTML report.

This is the entry point for the `report` skill usecase (see
`../report/SKILL.md`): standalone JSON/HTML report maintenance (re-render
the HTML, or manually append a run). No device/serial console involved.
Only needed when `capture.py` isn't being run, since `capture.py` already
records and renders its own data as part of collecting it.

Deliberately kept deterministic, unlike flash.py/capture.py's retry/poll
loops (see their module docstrings): this module's own docstring makes
byte-for-byte reproducibility a documented contract --
"bootchart-overview-<slug>.html is ALWAYS fully derived from the JSON by
this script -- never hand-authored. Re-running `render` regenerates it
byte-for-byte from whatever is currently in the JSON." Randomized output
here would silently break that contract (every render of the same JSON
should diff clean against the last) for no benefit -- there is no hardware
race to spread retries across, since no device or network call happens in
this module at all.

Run with the ARM64 Python launcher on this machine (`py -3`), or with a
regular Windows Python install (`python3`):
    py -3 report.py --report-cmd render --target iq-9075-evk
    py -3 report.py --report-cmd add-run --run-json new_run.json --target iq-9075-evk
    py -3 report.py --report-cmd set-analysis --target iq-9075-evk --log-name default-1 \
        --overheads-file overheads.txt --optimization-file optimization.txt

bootchart-data-<slug>.json is the only file ever hand-edited (by appending a
run). bootchart-overview-<slug>.html is ALWAYS fully derived from the JSON
by this script -- never hand-authored. Re-running `render` regenerates it
byte-for-byte from whatever is currently in the JSON.

Run object schema:
{
  "timestamp": "YYYY-MM-DD HH:MM",
  "metrics": {
    "nhlos":           {"value": "5.035 s", "note": "4.318 s firmware + 0.717 s loader"},
    "kernel":          {"value": "1.573 s", "note": ""},
    "initramfs":       {"value": "0.458 s", "note": "0.092 s -> 0.550 s"},
    "sysinit_svc":     {"value": "2.734 s", "note": "0.550 s -> 3.284 s",
                         "hitters": [{"name": "systemd-tmpfiles-setup.service", "time": "0.057 s"}, ...]},
    "total_sysinit":   {"value": "9.342 s", "note": ""},
    "total_multiuser": {"value": "19.005 s", "note": "~19.79 s incl. graphical.target",
                         "hitters": [{"name": "android-tools-adbd.service", "time": "10.416 s"}, ...]}
  },
  "critical_chain": ["docker.service", "network-online.target", "..."]   // optional, kept for backup/.txt context
}
A metric row's `hitters` list (where present) IS rank -- position 0 is #1, NOT a fixed identity
across runs. NHLOS/kernel/initramfs carry no `hitters` key: there's no per-component timing source
for them today (no initcall_debug on this build, no bootloader trace).

Older entries from before hitters were merged into the metrics table may still carry a flat
top-level "hitters" list instead of a nested one. Rendering falls back to treating that as
total_multiuser's hitters, so no data migration is needed.

A per-run "device_info" (sibling of "timestamp"/"build_path"/"boots" within a run entry),
set from flash.collect_device_info() right after login during that run's capture, plus a
"serial" field set separately by capture.py from `adb devices`'s own device id once adb is
up (NOT a sysfs/device-read value -- see capture.py's wait_for_adb_device) -- NOT a
top-level field, since Machine/SoC/OS/Kernel/Serial/Captured genuinely differ build-to-build (a
device reflashed with a new nightly can carry a different kernel/OS version):
{
  "timestamp": "2026-10-01 14:32", "build_path": "...", "boots": [...],
  "device_info": {
    "machine": "Qualcomm Technologies, Inc. Lemans EVK",
    "soc": "QCS9075 rev 2.0",
    "os": "Qualcomm Linux Reference Distro (SELinux-enabled) (OTA-enabled)",
    "kernel": "7.2.0-01462-gd49c33864d06",
    "serial": "0123456789ABCDEF",
    "captured": "2026-10-01 14:32:05"
  }
}
Serial is the device id exactly as printed in `adb devices`' first column (what `adb -s
<serial>` would target) -- not anything read off the device over serial console, since that
id (e.g. a SoC-level serial_number) isn't what adb/fastboot actually address the device by.
Rendered as its own "Device Information" table above the Boot Time Summary table, one column
per captured build (run) -- same column styling as the metrics table. Any field that couldn't
be read from the device is "-"; a run with no device_info at all (older entries, or add-run)
shows "-" for every field in its column; if NO run in the whole history carries device_info,
the table is omitted entirely. A legacy top-level "device_info" (from an earlier version that
stored one snapshot, overwritten every capture) is migrated onto the latest run on load.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path, PureWindowsPath

BASE_DIR = Path(__file__).resolve().parent
BOOT_CHARTS_DIR = BASE_DIR / "Boot-Charts"  # flat: every device's bootchart-data/-overview files land directly here, no per-device subdir
DEFAULT_DATA = BOOT_CHARTS_DIR / "bootchart-data.json"
DEFAULT_HTML = BOOT_CHARTS_DIR / "bootchart-overview.html"
BOOT_LOGS_DIR = BASE_DIR / "Boot-Logs"  # nested Boot-Logs/<target>/<build-folder>/<timestamp>.txt

MAX_RUNS = 30  # bootchart-data.json / .html keep only the most recent N runs;
               # full history lives on in Boot-Logs/*.txt regardless of this cap.

METRIC_ROWS = [
    ("nhlos", "NHLOS (PBL/SBL/XBL firmware + ABL loader)", False),
    ("kernel", "Kernel time", False),
    ("initramfs", "Initramfs (<code>Run /init</code> &rarr; epoch advance)", False),
    ("sysinit_svc", "systemd &rarr; sysinit.target", False),
    ("total_sysinit", "Total time till sysinit.target", True),
    ("total_multiuser", "Total time till multi-user.target", True),
]

# Notes that are identical on every boot (constant text, not derived per-run)
# are shown once under the row's own label instead of being repeated in every
# cell -- see render_metrics_table / _hitters_html.
ROW_CAPTIONS = {
    "initramfs": "dmesg: Run /init → System time advanced to built-in epoch",
    "sysinit_svc": "dmesg epoch advance → journalctl: Reached target System Initialization",
}

DELTA_THRESHOLD = 0.5
TIME_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def parse_seconds(value):
    if not value:
        return None
    m = TIME_RE.search(value)
    return float(m.group()) if m else None


def format_delta(prev_value, cur_value):
    """Return (label, css_class) for the delta annotation, or ("", "") if not computable."""
    p, c = parse_seconds(prev_value), parse_seconds(cur_value)
    if p is None or c is None:
        return "", ""
    delta = c - p
    if delta >= DELTA_THRESHOLD:
        cls = "regressed"
    elif delta <= -DELTA_THRESHOLD:
        cls = "improved"
    else:
        cls = "unchanged"
    sign = "+" if delta >= 0 else "−"
    return f"({sign}{abs(delta):.3f} s)", cls


def esc(value):
    return html.escape(str(value), quote=False)


def load_data(data_path):
    if not data_path.exists():
        return {"device": "unknown", "runs": []}
    with open(data_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # One-time migration: device_info used to be stored once at the top level
    # (overwritten on every capture), which lost the fact that OS/kernel/
    # captured-time genuinely differ build-to-build. It's now stored per-run
    # (see add_run / pull_and_record). Attach any legacy top-level snapshot to
    # the most recent run instead of discarding it, then drop the stale key.
    legacy_device_info = data.pop("device_info", None)
    if legacy_device_info and data.get("runs"):
        data["runs"][-1].setdefault("device_info", legacy_device_info)
    return data


def save_data(data, data_path):
    data_path.parent.mkdir(parents=True, exist_ok=True)
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _validate_boot(boot):
    required = {"timestamp", "metrics"}
    missing = required - boot.keys()
    if missing:
        raise ValueError(f"boot object missing required keys: {missing}")
    missing_metrics = {k for k, _, _ in METRIC_ROWS} - boot["metrics"].keys()
    if missing_metrics:
        raise ValueError(f"boot.metrics missing required keys: {missing_metrics}")


def add_run(data, run):
    if "boots" in run:
        required_top = {"timestamp", "build_path", "boots"}
        missing = required_top - run.keys()
        if missing:
            raise ValueError(f"multi-boot entry missing required keys: {missing}")
        if not run["boots"]:
            raise ValueError("multi-boot entry has an empty 'boots' list")
        for boot in run["boots"]:
            _validate_boot(boot)
    else:
        _validate_boot(run)
    runs = data.setdefault("runs", [])
    runs.append(run)
    del runs[:-MAX_RUNS]  # trim from the front; full history is preserved in Boot-Logs/*.txt
    return data


def _flatten(runs):
    """Expands entries into a flat list of per-boot records, normalizing
    legacy flat (single-boot) entries to a 1-item boots list. Each record:
    {"boot": <boot dict>, "idx": <position within its entry>, "n": <boots in that entry>}.
    All rendering iterates this flat list so delta/rank/footer logic doesn't
    need to special-case entry boundaries -- boot 0 of entry K just diffs
    against the last boot of entry K-1, like any two consecutive boots."""
    flat = []
    for run in runs:
        boots = run.get("boots") or [run]
        n = len(boots)
        for idx, boot in enumerate(boots):
            flat.append({"boot": boot, "idx": idx, "n": n})
    return flat


def _metric_hitters(boot, key):
    """Returns the hitters list for a given metric row, or [] if none.
    Legacy entries (captured before hitters were merged into the metrics
    table) carry a flat top-level "hitters" list -- treated as
    total_multiuser's hitters, since that's what it always meant before."""
    m = boot["metrics"].get(key, {})
    if "hitters" in m:
        return m["hitters"]
    if key == "total_multiuser":
        return boot.get("hitters") or []
    return []


def _format_run_txt(target, run):
    """Human-readable dump of a single run, for the Boot-Logs/ backup -- the
    full-history record that survives bootchart-data.json's MAX_RUNS trim."""
    lines = [
        f"Device: {target}",
        f"Timestamp: {run['timestamp']}",
        f"Build path: {run.get('build_path', '')}",
    ]
    if run.get("phase"):
        lines.append(f"Phase: {run['phase']} (boot {run.get('log_name', '').rsplit('-', 1)[-1]})")
    lines += ["", "Metrics:"]
    for key, label, _ in METRIC_ROWS:
        m = run["metrics"].get(key, {})
        note = f"  ({m['note']})" if m.get("note") else ""
        lines.append(f"  {label}: {m.get('value', '')}{note}")
        for i, h in enumerate(_metric_hitters(run, key)):
            h_note = f"  ({h['note']})" if h.get("note") else ""
            lines.append(f"    #{i + 1}. {h['name']}: {h['time']}{h_note}")

    chain = run.get("critical_chain")
    if chain:
        lines.append("")
        lines.append("Critical chain: " + " -> ".join(chain))

    overall_overheads_text = run.get("overall_overheads_text")
    if overall_overheads_text:
        lines.append("")
        lines.append("Overall Suspected Overheads:")
        lines.append(overall_overheads_text)
    else:
        overall_overheads = run.get("overall_overheads")
        if overall_overheads:
            lines.append("")
            lines.append("Overall Suspected Overheads:")
            for i, h in enumerate(overall_overheads):
                h_note = f"  ({h['note']})" if h.get("note") else ""
                lines.append(f"  #{i + 1}. {h['name']}: {h['time']}{h_note}")

    if run.get("optimization_possibilities"):
        lines.append("")
        lines.append("Optimization possibilities: " + run["optimization_possibilities"])

    if run.get("source"):
        lines.append("")
        lines.append("Source: " + run["source"])

    return "\n".join(lines) + "\n"


def boot_logs_run_dir(target, build_path, boot_logs_dir=BOOT_LOGS_DIR):
    """Returns the nested Boot-Logs/<target>/<build-folder-name> directory for
    a given target + build_path, shared by save_run_backup's per-boot .txt
    backups and boot-capture's pulled raw-log folders (Logs-1, Logs-2, ...) so
    both land side-by-side in the same directory."""
    return boot_logs_dir / target / _build_folder_name(build_path) if build_path else boot_logs_dir


def save_run_backup(target, run, boot_logs_dir=BOOT_LOGS_DIR):
    """Writes a permanent per-run backup to
    Boot-Logs/<target>/<build-folder-name>/<timestamp>.txt, since
    bootchart-data.json only retains the most recent MAX_RUNS entries. Adds a
    numeric suffix if a backup for the same minute-granularity timestamp
    already exists (e.g. consecutive boots in a multi-boot entry can finish
    within the same minute), so no boot's backup is silently overwritten.
    Pre-existing flat backups directly under boot_logs_dir (from before this
    nested layout) are left in place -- only new backups go into the nested
    <target>/<build-folder> path."""
    build_path = run.get("build_path", "")
    run_dir = boot_logs_run_dir(target, build_path, boot_logs_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    safe_ts = run["timestamp"].replace(":", "-")
    txt_path = run_dir / f"{safe_ts}.txt"
    n = 2
    while txt_path.exists():
        txt_path = run_dir / f"{safe_ts}_{n}.txt"
        n += 1
    txt_path.write_text(_format_run_txt(target, run), encoding="utf-8")
    return txt_path


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

CSS = """
  :root {
    --bg: #f4f6f9;
    --panel: #ffffff;
    --panel-alt: #eef3fa;
    --border: #c8d8ea;
    --text: #1a2733;
    --muted: #5a6b7d;
    --accent: #2c6fad;
    --accent-dark: #1a3a5c;
    --good: #1e7e34;
    --good-bg: #e8f8e8;
    --warn: #8a6d1d;
    --warn-bg: #fef8e0;
    --bad: #b02a37;
    --bad-bg: #fde8e8;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0;
    padding: 0;
    background: var(--bg);
    color: var(--text);
    font-family: "Segoe UI", -apple-system, Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.5;
    overflow-x: auto;
  }
  .container { padding: 40px 20px; width: max-content; min-width: 100%; }
  h1 { font-size: 1.6rem; color: var(--accent-dark); margin-bottom: 10px; border-bottom: 3px solid var(--accent); padding-bottom: 10px; }
  .subtitle { color: var(--muted); font-size: 0.95rem; margin-bottom: 28px; max-width: 1100px; }
  .subtitle code { background: var(--panel-alt); color: var(--accent-dark); padding: 2px 6px; border-radius: 4px; border: 1px solid var(--border); }
  h2 { font-size: 1.1rem; color: var(--accent-dark); margin-top: 36px; margin-bottom: 12px; border-left: 3px solid var(--accent); padding-left: 10px; }
  table { border-collapse: collapse; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,0.07); }
  th, td { text-align: left; padding: 12px 19px; border: 1px solid var(--border); white-space: nowrap; }
  th:first-child, td:first-child { width: 260px; white-space: normal; }
  th { background: var(--accent-dark); color: #ffffff; font-size: 0.8rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; }
  th.run-col { color: #eaf1fb; text-transform: none; letter-spacing: normal; font-size: 0.85rem; min-width: 520px; }
  th.run-col.latest { border-bottom: 3px solid #7db3e8; }
  tbody tr:not(.total):nth-child(odd) td { background: #f4f8fc; }
  tbody tr:not(.total):nth-child(even) td { background: #ffffff; }
  tr.total td { font-weight: 600; color: var(--text); background: #e4edf8; border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); }
  td.timing { font-family: "SF Mono", Consolas, monospace; font-size: 0.95rem; }
  td.timing .note { display: block; color: var(--muted); font-size: 0.78rem; font-family: "Segoe UI", -apple-system, Roboto, Helvetica, Arial, sans-serif; white-space: normal; margin-top: 2px; }
  td.timing .delta { font-size: 0.78rem; margin-left: 6px; padding: 1px 6px; border-radius: 10px; font-family: "SF Mono", Consolas, monospace; }
  .delta.unchanged { color: var(--muted); background: var(--panel-alt); }
  .delta.improved { color: var(--good); background: var(--good-bg); }
  .delta.regressed { color: var(--bad); background: var(--bad-bg); }
  td.buildpath { font-family: "SF Mono", Consolas, monospace; font-size: 0.78rem; color: var(--muted); white-space: normal; word-break: break-all; text-align: center; }
  th.entry-start, td.entry-start { border-left: 3px solid var(--accent); padding-left: 20px; }
  th.phase-start, td.phase-start { padding-left: 20px; }
  td:first-child .row-caption { display: block; font-weight: 400; font-size: 0.75rem; color: var(--muted); margin-top: 2px; white-space: normal; }
  th .row-caption { display: block; font-weight: 400; font-size: 0.75rem; color: #cddcee; margin-top: 2px; white-space: normal; }
  td.timing ol.hitters { list-style: none; margin: 6px 0 0; padding: 0; white-space: normal; }
  td.timing ol.hitters li { font-size: 0.78rem; font-family: "Segoe UI", -apple-system, Roboto, Helvetica, Arial, sans-serif; color: var(--muted); margin-top: 2px; }
  td.timing ol.hitters li .hitter-name { font-family: "SF Mono", Consolas, monospace; color: var(--accent-dark); }
  td.optimization { white-space: normal; max-width: 420px; vertical-align: top; }
  td.optimization .optimization-text { font-size: 0.85rem; line-height: 1.5; white-space: normal; word-wrap: break-word; }
  td.optimization ul.optimization-text { margin: 0; padding-left: 18px; }
  td.optimization ul.optimization-text li { margin-bottom: 6px; }
  td.na { color: var(--muted); text-align: center; }
"""


def _boot_header_label(rec):
    boot = rec["boot"]
    if rec["n"] == 1:
        return boot["timestamp"]
    log_name = boot.get("log_name")
    if log_name:
        phase, _n = log_name.rsplit("-", 1)
        return phase.capitalize()
    time_part = boot["timestamp"].split(" ")[-1]
    return f"Boot {rec['idx'] + 1} · {time_part}"


def _boot_header_subtitle(rec):
    """Returns a small subtitle string to show under a column header, or
    None. Content-based (checks metrics.kernel.hitters), not a threaded
    flag -- correct even under --resume-pull, which never calls
    ensure_debug_cmdline_params() at all."""
    boot = rec["boot"]
    if boot.get("phase") != "debug":
        return None
    if boot.get("metrics", {}).get("kernel", {}).get("hitters"):
        return None
    return "No debug prints enabled"


def _phase_boundary(flat, i):
    """True when boot i starts a new phase (e.g. debug right after default)
    within the same multi-boot entry -- used to draw a visual separator
    between the default and debug columns, distinct from entry-start's
    separator between different captured runs."""
    if i == 0:
        return False
    prev_phase = flat[i - 1]["boot"].get("phase")
    cur_phase = flat[i]["boot"].get("phase")
    return bool(prev_phase and cur_phase and prev_phase != cur_phase)


def _col_extra_class(flat, i, rec):
    classes = []
    if rec["idx"] == 0 and i > 0:
        classes.append("entry-start")
    elif _phase_boundary(flat, i):
        classes.append("phase-start")
    return (" " + " ".join(classes)) if classes else ""


def _header_cells(flat):
    return "".join(
        f'<th class="run-col{" latest" if i == len(flat) - 1 else ""}'
        f'{_col_extra_class(flat, i, rec)}">'
        f'{esc(_boot_header_label(rec))}'
        + (
            f'<span class="row-caption">{esc(_boot_header_subtitle(rec))}</span>'
            if _boot_header_subtitle(rec) else ""
        )
        + '</th>'
        for i, rec in enumerate(flat)
    )


def _strip_performance_suffix(build_path):
    """Boot analysis is only ever run against a build's '...\\performance'
    subdirectory, so that segment is implied and dropped from display/paths."""
    p = PureWindowsPath(build_path)
    if p.name.lower() == "performance":
        p = p.parent
    return str(p)


def _build_folder_name(build_path):
    return PureWindowsPath(_strip_performance_suffix(build_path)).name or "unknown-build"


def render_build_path_row(runs):
    if not any(r.get("build_path") for r in runs):
        return ""
    cells = "".join(
        f'<td class="buildpath" colspan="{len(run.get("boots") or [run])}">{esc(_strip_performance_suffix(run.get("build_path", "")))}</td>'
        for run in runs
    )
    return f"<tr><td>Nightly build path</td>{cells}</tr>"


def _hitters_html(hitters):
    """Ranked sub-list rendered inside a metric's value cell. Each hitter's
    "note" (e.g. "largest single cost", "critical path"/"off critical path")
    is precomputed at capture time in capture.build_run, not here."""
    if not hitters:
        return ""
    items = []
    for i, h in enumerate(hitters):
        note = h.get("note", "")
        note_html = f" &middot; {esc(note)}" if note else ""
        items.append(
            f'<li class="rank-{i + 1}">#{i + 1} <span class="hitter-name">{esc(h["name"])}</span> '
            f'&mdash; {esc(h["time"])}{note_html}</li>'
        )
    return f'<ol class="hitters">{"".join(items)}</ol>'


def _optimization_html(text):
    """Renders optimization_possibilities as a bulleted list, one <li> per
    newline-separated point (each point names a service/probe hitter and its
    suggested fix -- see SKILL.md workflow step 4). Falls back to a single
    wrapped block for older entries that are still one paragraph (no
    newlines)."""
    lines = [l.strip().lstrip("•").strip() for l in text.splitlines() if l.strip()]
    if len(lines) <= 1:
        return f'<div class="optimization-text">{esc(text)}</div>'
    items = "".join(f"<li>{esc(line)}</li>" for line in lines)
    return f'<ul class="optimization-text">{items}</ul>'


def render_overall_rows(flat):
    """Two rows appended below the per-stage metric rows: 'Overall Suspected
    Overheads' (agent-authored prose from regression_analysis findings --
    outliers, regressions, high hitters, RCA -- see
    capture/reference/COT_outlier_detection.md) and 'Optimization
    Possibilities' (agent-authored fix recommendations, see
    capture/reference/COT_optimization_recommendations.md). Both render
    through the same simple bullet-per-line helper (_optimization_html) --
    deliberately no tables/charts here, just plain text, so these two cells
    stay easy to read regardless of how much the underlying analysis found.
    Falls back to the legacy pooled-hitters list (_hitters_html) for older
    entries captured before overall_overheads_text existed."""
    overhead_cells = []
    optimization_cells = []
    for i, rec in enumerate(flat):
        boot = rec["boot"]
        cls = _col_extra_class(flat, i, rec)

        overheads_text = boot.get("overall_overheads_text")
        if overheads_text:
            overhead_html = _optimization_html(overheads_text)
        else:
            legacy_overheads = boot.get("overall_overheads") or []
            overhead_html = _hitters_html(legacy_overheads) if legacy_overheads else '<span class="na">–</span>'
        overhead_cells.append(f'<td class="timing optimization{cls}">{overhead_html}</td>')

        optimization = boot.get("optimization_possibilities")
        optimization_html = _optimization_html(optimization) if optimization else '<span class="na">–</span>'
        optimization_cells.append(f'<td class="timing optimization{cls}">{optimization_html}</td>')

    overhead_row = f"<tr><td>Overall Suspected Overheads</td>{''.join(overhead_cells)}</tr>"
    optimization_row = f"<tr><td>Optimization Possibilities</td>{''.join(optimization_cells)}</tr>"
    return overhead_row + optimization_row


def render_metrics_table(runs):
    flat = _flatten(runs)
    header_cells = _header_cells(flat)
    rows_html = [render_build_path_row(runs)]
    for key, label, is_total in METRIC_ROWS:
        caption = ROW_CAPTIONS.get(key)
        label_html = f'{label}<span class="row-caption">{esc(caption)}</span>' if caption else label
        cells = []
        for i, rec in enumerate(flat):
            boot = rec["boot"]
            m = boot["metrics"][key]
            value, note = m.get("value", ""), m.get("note", "")
            delta_html = ""
            if i > 0:
                prev_value = flat[i - 1]["boot"]["metrics"][key].get("value", "")
                delta_label, delta_cls = format_delta(prev_value, value)
                if delta_label:
                    delta_html = f'<span class="delta {delta_cls}">{delta_label}</span>'
            note_html = f'<span class="note">{esc(note)}</span>' if note and note != caption else ""
            hitters_html = _hitters_html(_metric_hitters(boot, key))
            cls = "timing" + _col_extra_class(flat, i, rec)
            cells.append(f'<td class="{cls}">{esc(value)}{delta_html}{note_html}{hitters_html}</td>')
        row_class = ' class="total"' if is_total else ""
        rows_html.append(f"<tr{row_class}><td>{label_html}</td>{''.join(cells)}</tr>")
    rows_html.append(render_overall_rows(flat))
    return f"""
  <table>
    <thead><tr><th>Stage</th>{header_cells}</tr></thead>
    <tbody>
      {''.join(rows_html)}
    </tbody>
  </table>"""


DEVICE_INFO_FIELDS = [
    ("machine", "Machine"),
    ("soc", "SoC"),
    ("os", "OS"),
    ("kernel", "Kernel"),
    ("serial", "Serial"),
    ("captured", "Captured"),
]


def render_device_info_table(runs):
    """Renders the 'Device Information' table -- styled identically to the
    Boot Time Summary table (same CSS classes: table/th.run-col/td.timing),
    with one column PER CAPTURED BUILD (run), not per boot -- Machine/SoC/OS/
    Kernel/Serial/Captured don't differ between the default and debug boots
    of the same capture, so showing them once per build (rather than once
    per boot, like the metrics table) avoids duplicate columns. Each run's
    header/cells carry colspan=len(run["boots"]) -- the same technique
    render_build_path_row uses -- so this table's columns sit directly above
    that run's Default/Debug columns in the Boot Time Summary table below,
    rather than drifting out of alignment. The build path itself is NOT
    repeated here -- it's already shown once, in that same aligned position,
    by render_build_path_row in the metrics table. Returns "" when no run in
    the history carries device_info at all (older entries predating this
    field, or add-run-only JSON), so that JSON renders with no panel, exactly
    as before this field existed. A run missing device_info (mixed old/new
    history) just shows "–" in its own column instead of hiding the whole
    table."""
    if not any(r.get("device_info") for r in runs):
        return ""
    # th.run-col's 520px min-width applies per <th>/<td>; a merged cell spanning
    # n boot columns needs n*520px plus the (n-1) borders collapsed between
    # them, or it renders narrower than the n separate Default/Debug columns
    # it sits above in the metrics table -- misaligning the two tables.
    def _span_style(n):
        return f' style="min-width:{520 * n + (n - 1)}px"' if n > 1 else ""

    header_cells = "".join(
        # entry-start matches the thicker accent border the metrics table
        # draws between separate runs (_col_extra_class) -- without it, every
        # run boundary here is a few px narrower than its counterpart below,
        # and that drift compounds across runs.
        f'<th class="run-col{" latest" if i == len(runs) - 1 else ""}{" entry-start" if i > 0 else ""}" colspan="{len(run.get("boots") or [run])}"{_span_style(len(run.get("boots") or [run]))}>{esc(run["timestamp"])}</th>'
        for i, run in enumerate(runs)
    )
    rows_html = []
    for key, label in DEVICE_INFO_FIELDS:
        cells = "".join(
            f'<td class="timing{" entry-start" if i > 0 else ""}" colspan="{len(run.get("boots") or [run])}"{_span_style(len(run.get("boots") or [run]))}>{esc((run.get("device_info") or {}).get(key) or "–")}</td>'
            for i, run in enumerate(runs)
        )
        rows_html.append(f"<tr><td>{label}</td>{cells}</tr>")
    return f"""
  <table>
    <thead><tr><th>Field</th>{header_cells}</tr></thead>
    <tbody>
      {''.join(rows_html)}
    </tbody>
  </table>"""


def render_html(data):
    runs = data.get("runs", [])
    device = esc(data.get("device", "unknown device"))
    if not runs:
        body = "<p>No runs collected yet. Run <code>report.py --report-cmd add-run --run-json &lt;run.json&gt;</code> to add the first one.</p>"
        metrics_html = ""
        device_info_html = ""
    else:
        metrics_html = render_metrics_table(runs)
        device_info_html = render_device_info_table(runs)

    device_info_section = (
        f'\n  <h2>Device Information</h2>\n  {device_info_html}\n'
        if device_info_html else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Boot Time Analysis &mdash; {device}</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">

  <h1>Boot Time Analysis</h1>
  <div class="subtitle">Device <code>{device}</code> &mdash; history of collected boot runs. Data source: <code>bootchart-data.json</code>, generated by <code>report.py</code>. <b>Boot analysis is performed only on performance builds.</b> High hitters for each stage (where measurable) are ranked inline beneath that stage's timing.</div>
{device_info_section}
  <h2>Boot Time Summary</h2>
  {metrics_html}

</div>
</body>
</html>
"""


def render(data_path=DEFAULT_DATA, html_path=DEFAULT_HTML):
    data = load_data(data_path)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_html(data), encoding="utf-8")
    print(f"Rendered {html_path} from {data_path} ({len(data.get('runs', []))} run(s)).")


def _slugify(target: str) -> str:
    return target.replace("-", "").replace("_", "")


def _data_html_paths(target: str):
    slug = _slugify(target)
    base = BOOT_CHARTS_DIR
    return base / f"bootchart-data-{slug}.json", base / f"bootchart-overview-{slug}.html"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def add_report_args(parser):
    """Adds report.py's own-specific flags (not --target, which flash.py/
    capture.py/report.py each define directly, since all three need it
    standalone) to an existing parser. Shared with flash.py's --all parser
    so --report-cmd/--run-json aren't redefined by hand there."""
    report_group = parser.add_argument_group("report")
    report_group.add_argument("--report-cmd", choices=["render", "add-run", "set-analysis"], default="render", help="'render' (default) just regenerates the HTML from the existing JSON; 'add-run' appends --run-json first; 'set-analysis' writes agent-authored overall_overheads_text/optimization_possibilities onto a boot in the most recent run, then re-renders")
    report_group.add_argument("--run-json", help="Path to a run JSON to append (or '-' for stdin), used with --report-cmd add-run")
    report_group.add_argument("--log-name", help="log_name of the boot to annotate (e.g. 'default-1', 'debug-1') in the most recently recorded run, used with --report-cmd set-analysis")
    report_group.add_argument("--overheads-file", help="Path to a text file whose contents become the named boot's overall_overheads_text, used with --report-cmd set-analysis")
    report_group.add_argument("--optimization-file", help="Path to a text file whose contents become the named boot's optimization_possibilities, used with --report-cmd set-analysis")
    return parser


def cmd_report(args):
    """Standalone report maintenance: (optionally) append a run JSON or set
    agent-authored analysis text on a boot, then re-render the HTML from the
    JSON. No device involved."""
    if not args.target:
        print("report.py requires --target.")
        sys.exit(1)

    data_path, html_path = _data_html_paths(args.target)
    if args.report_cmd == "add-run":
        if not args.run_json:
            print("--report-cmd add-run requires --run-json <path|->")
            sys.exit(1)
        raw = sys.stdin.read() if args.run_json == "-" else Path(args.run_json).read_text(encoding="utf-8")
        run = json.loads(raw)
        data = load_data(data_path)
        add_run(data, run)
        save_data(data, data_path)
        print(f"Appended run -> {data_path}")

    elif args.report_cmd == "set-analysis":
        if not args.log_name:
            print("--report-cmd set-analysis requires --log-name <log_name>")
            sys.exit(1)
        if not args.overheads_file and not args.optimization_file:
            print("--report-cmd set-analysis requires --overheads-file and/or --optimization-file")
            sys.exit(1)

        data = load_data(data_path)
        if not data.get("runs"):
            print(f"No runs recorded yet for target {args.target!r}; nothing to annotate.")
            sys.exit(1)

        latest_run = data["runs"][-1]
        boot = next((b for b in (latest_run.get("boots") or [latest_run]) if b.get("log_name") == args.log_name), None)
        if boot is None:
            available = [b.get("log_name") for b in (latest_run.get("boots") or [latest_run])]
            print(f"No boot with log_name={args.log_name!r} found in the most recent run (available: {available}).")
            sys.exit(1)

        if args.overheads_file:
            boot["overall_overheads_text"] = Path(args.overheads_file).read_text(encoding="utf-8")
        if args.optimization_file:
            boot["optimization_possibilities"] = Path(args.optimization_file).read_text(encoding="utf-8")

        save_data(data, data_path)
        print(f"Updated analysis for boot {args.log_name!r} -> {data_path}")

    render(data_path, html_path)
    print(f"Report regenerated -> {html_path}")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="report.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--target", help="Target name (e.g. iq-9075-evk), used to resolve bootchart-data-<slug>.json / bootchart-overview-<slug>.html. Required.")
    add_report_args(parser)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    cmd_report(args)


if __name__ == "__main__":
    main()
