# bootbench.py -- data model & report internals

## Source-of-truth rule

`Boot-Charts/bootchart-data-<slug>.json` is the **only** file ever written directly (by
`capture`, or by hand via `report --report-cmd add-run`). `Boot-Charts/bootchart-overview-<slug>.html`
is **always** fully derived from that JSON by `bootbench.py` -- never hand-authored or
hand-patched. Re-running `report --report-cmd render` regenerates the HTML byte-for-byte from
whatever is currently in the JSON. `<slug>` is the target name with hyphens/underscores
stripped (e.g. `iq-9075-evk` -> `iq9075evk`).

## Run entry schema

A "run" appended to the JSON's `runs` array is either a single boot (legacy/flat form) or a
multi-boot entry (current form, produced by `capture`):

```json
{
  "timestamp": "YYYY-MM-DD HH:MM",
  "build_path": "\\\\swayam\\...\\performance",
  "boots": [
    {
      "timestamp": "YYYY-MM-DD HH:MM",
      "build_path": "\\\\swayam\\...\\performance",
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
      "critical_chain": ["docker.service", "network-online.target", "..."],
      "source": "..."
    },
    ...
  ]
}
```

- `metrics` keys (`nhlos`, `kernel`, `initramfs`, `sysinit_svc`, `total_sysinit`,
  `total_multiuser`) are **fixed row labels** across every run -- they line up as rows in the
  Boot Time Summary table regardless of which run/boot column you're looking at.
- `nhlos`, `kernel`, and `initramfs` never carry a `hitters` key -- there's no per-component
  timing source for them (no `initcall_debug`, no bootloader trace).
- `critical_chain` and `source` are optional, kept for backup/`.txt`-dump context.

## Rank, not identity

Wherever a `hitters` list is present, **array position IS rank** -- index 0 is rank #1 for
that specific boot, not a fixed identity across runs. The High Hitters HTML output is keyed by
rank (#1, #2, ...), and each run's column independently shows whichever service actually held
that rank in that boot. Do not union or fix service names into rows: which services appear,
and in what order, changes between boots (e.g. `weston.service` vs.
`systemd-udev-trigger.service` swapping at rank #6 across real runs is expected, not a bug).

## Legacy flat `hitters`

Older entries captured before hitters were merged into the per-metric structure may carry a
flat top-level `"hitters"` list instead of a nested one under `total_multiuser`. Rendering
falls back to treating that flat list as `total_multiuser`'s hitters, so no data migration is
required to keep old entries readable.

## Retention

- `Boot-Charts/bootchart-data-<slug>.json` keeps only the most recent `MAX_RUNS` (30) run
  entries -- older ones are trimmed from the front on every `add_run`.
- Full history is preserved regardless of that cap in
  `Boot-Logs/<target>/<build-folder>/<timestamp>.txt` -- one permanent backup per boot, never
  trimmed. If a timestamp collides within the same minute (e.g. consecutive boots in a
  multi-boot entry finishing in the same minute), a numeric suffix (`_2`, `_3`, ...) is added
  so no boot's backup is silently overwritten.

## Delta annotations

Consecutive runs' metric values are diffed per row. A change of `>= 0.5 s` between
consecutive runs is shown as a regression (`+`, red) or improvement (`-`, green); anything
smaller is shown as "unchanged" (grey) -- this is a noise threshold, not a claim that nothing
happened.

## Manually appending a run

`report --report-cmd add-run --run-json <file>` validates the run object (`_validate_boot` /
required keys) before appending, then re-renders the HTML. Always go through this path (or let
`capture` write it automatically) -- never open the JSON in an editor and add an entry by hand.
