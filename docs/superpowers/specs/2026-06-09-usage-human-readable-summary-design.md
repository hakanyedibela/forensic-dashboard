# Human-readable usage summary — design

Date: 2026-06-09
Tool: `scripts/python/fetch-cluster-usage.py`

## Problem

The usage report already computes peak CPU/mem and `oom_count` at every level
(cluster → stage → namespace → workload → pod → container) over the `--window`
lookback. Two things make it look like it doesn't:

1. **Window.** The published `reports-usage/` was generated with the default
   `--window 24h`. Seven days is simply `--window 7d` — no code change.
2. **Readability.** A `render_text()` table formatter already exists (cores,
   `Ki/Mi/Gi`, `%`, OOM count) but it is only written to **stdout**. When the
   tool writes to `--output-dir`, only the raw-number CSVs/JSON land on disk, so
   the folder shows values like `5.25e-07` cores and `339968.0` bytes.

## Decision

Keep the raw CSVs exactly as they are (machine-parseable, units in column
names). Add a **separate** human-readable artifact and improve small-CPU
formatting.

## Changes

1. **Write `summary.txt` to disk.** In `write_report_files()`, render
   `render_text()` into `<out_dir>/summary.txt` (combined) and into each
   `by-stage/<stage>/summary.txt`. Reuses the existing formatter; no new table
   logic. Per-stage files use `summary=False`-style scoping consistent with the
   existing per-stage CSV/JSON (stage rollup only).

2. **Kubernetes-style `fmt_cores()`.** Today `f"{v:.3f}"` flattens anything
   below ~1 milli-core to `0.000`. New behavior:
   - `None` → `"-"`, `0` → `"0"`
   - `|v| >= 1` → trimmed decimal, e.g. `24.5`, `1`
   - `1e-3 <= |v| < 1` → milli, e.g. `100m`, `2.8m`, `250m`
   - `|v| < 1e-3` → micro, e.g. `0.525µ`

   No scientific notation. Only affects `render_text` (the sole caller); CSV/JSON
   raw values are untouched. `bytes`/`%` formatters already read well — unchanged.

3. **LEGEND.** Add a `summary.txt` line to the embedded `LEGEND_TEXT`.

4. **Tests.** Update `test_fmt_cores` for the new milli/micro output; add cases
   for the milli/micro boundaries; add a test that `write_report_files()` emits
   `summary.txt` with formatted (non-scientific) content.

## Out of scope

- Reformatting the raw CSVs or adding `*_human` columns.
- Any change to metric collection, queries, or the `oom_count` logic.

## How the user runs it

```
scripts/python/fetch-cluster-usage.py --window 7d --output-dir reports-usage
```
