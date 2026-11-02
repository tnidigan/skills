# Bootbench — Flash / Capture / Report

Automates the Qualcomm target-device pipeline: find the latest nightly
build, flash it over EDL via PCAT, boot the device N times to collect
boot-time logs, parse them into metrics, and render/maintain an HTML boot
analysis report.

The pipeline is split into three independent stages (flash / capture /
report), each documented in its own `SKILL.md`, so an agent (or a human)
can invoke just the stage that's actually needed instead of always
running the whole thing. Only this root `SKILL.md` is agent-discoverable
(see "Repository Structure" below); the per-stage `SKILL.md` files are
reference documentation for whichever stage is actually run.

---

## Repository Structure

```
bootbench/                        ← this skill's root (installed at .claude/skills/bootbench/)
├── SKILL.md                        (agent-discoverable; this file)
├── flash/                          ← Skill: flash a device over EDL via PCAT
│   ├── SKILL.md
│   └── flash.py
├── capture/                        ← Skill: boot an already-flashed device, collect boot-time data
│   ├── SKILL.md                    
│   ├── capture.py                     ← see "Cross-module imports" note below
│   ├── regression_analysis.py         ← outlier/regression/high-hitter detection, co-located, imported directly
│   └── reference/                     ← COT docs guiding agent-authored analysis text
│       ├── COT_outlier_detection.md
│       ├── COT_regression_rca.md
│       └── COT_optimization_recommendations.md
└── report/                         ← Skill: regenerate/maintain the HTML report, no device involved
    ├── SKILL.md                       
    ├── report.py                      ← see "Cross-module imports" note below
    └── Boot-Charts/, Boot-Logs/        (generated output, created once a capture/report has run)
```

This file (`bootbench/SKILL.md`) is the one agent-discoverable skill
description for `bootbench` as a whole — it's what actually gets listed
and invoked as the `bootbench` skill. `flash/SKILL.md`, `capture/SKILL.md`,
and `report/SKILL.md` document each stage on its own (inputs, workflow,
outputs) for anyone reading this tree directly, but they are not
separately-registered skills — routing between the three stages below is
a judgment call made from this file plus each stage's own `SKILL.md`, not
automatic discovery of three independent skills.

## flash / capture / report are independent stages

`flash.py`, `capture.py`, and `report.py` are each meant to be run on their
own, as the entry point for their own stage — not as one monolithic
pipeline. Prefer invoking just the stage the user actually asked for:

- Only need to flash? Run `flash.py` with no `--all` — it never touches
  `capture.py`/`report.py` and works even if neither is present.
- Device already flashed, just want fresh boot numbers? Run `capture.py`
  on its own.
- No device involved, just want the report regenerated? Run `report.py`
  on its own.

`flash.py --all` (chaining straight into capture + report in-process) still
exists for convenience when a user explicitly asks for the whole pipeline
in one command, but it's the exception, not the default way these stages
are meant to be used.

## Cross-module imports: `capture.py` needs `flash.py` + `report.py`

`capture.py` has no serial-console or report-rendering logic of its own —
it does a plain `import flash` and `import report` and delegates to them
for everything device- and report-related. `flash.py --all` similarly does
`import capture` / `import report`, but only when `--all` is actually
passed (see `flash.py`'s `build_parser()` — the import is skipped
entirely for a bare `flash.py` invocation, so a flash-only deployment
never needs `capture.py`/`report.py` present at all).

These imports work under **either** directory layout:

- **Per-usecase** — each script living in its own sibling skill folder,
  e.g. `.../flash/flash.py`, `.../capture/capture.py`,
  `.../report/report.py` (how this installed `bootbench` skill tree
  actually has them — see "Repository Structure" above)
- **Flat** — `flash.py`/`capture.py`/`report.py` side by side in one
  folder (an alternative deployment layout the same scripts also support)

Each of the three scripts has an `_ensure_importable(module_name)` helper
that checks both `<its own folder>/<module_name>.py` and
`<parent of its own folder>/<module_name>/<module_name>.py`, and inserts
whichever one actually exists onto `sys.path` before the real
`import <module_name>`. That's a lightweight shim, not real packaging — it
exists so moving these scripts between the two layouts above never breaks
the required imports, without needing to refactor the shared logic into an
installable package.

---

## Stages

| Stage | Picks this when the user wants to... | Status |
|---|---|---|
| **[flash](flash/SKILL.md)** | Flash a target device with a (latest or specific) nightly build over EDL. Can optionally chain into capture + report via `--all`, but is a complete standalone stage on its own | Implemented |
| **[capture](capture/SKILL.md)** | Boot an already-flashed device N times, collect/parse boot-time logs, and run outlier/regression/root-cause analysis into the report, without reflashing | Implemented |
| **[report](report/SKILL.md)** | Re-render the HTML report from existing JSON history, manually append a run JSON, or write agent-authored analysis text onto a boot (`set-analysis`) — no device involved | Implemented |

### Routing

- Device needs to be reflashed (new build, wrong build, or just "flash it") → **flash** (standalone)
- Device is already flashed and the user wants fresh boot-time numbers → **capture** (standalone)
- No device involved at all — just regenerate/append to the existing report → **report** (standalone)
- User explicitly wants the whole pipeline in one go ("flash and give me the boot report") → **flash** with `--all` (chains into capture, which itself chains into report) — the one case where running these as a single command, rather than three separate stage invocations, is appropriate

Each stage's own `SKILL.md` is the source of truth for its inputs,
workflow, and outputs.

## Prerequisites

```
py -3 -m pip install pyserial comtypes
```

or

```
python3 -m pip install pyserial comtypes
```

- `pyserial` — required by `flash` and `capture` (serial console login,
  command execution, port auto-detection)
- `comtypes` — required by `flash` only (Alpaca TAC COM automation for
  power-cycle / EDL entry)
- `adb` must be on `PATH` — used by `capture` to pull logs off the device

Run with this machine's ARM64 Python launcher (`py -3 ...`), or a regular
Windows Python install (`python3 ...`).
