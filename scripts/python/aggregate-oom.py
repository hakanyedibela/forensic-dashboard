#!/usr/bin/env python3
"""
aggregate-oom.py

Reads every report.json produced by fetch-cluster-oom-loop.sh and produces:

  * _oom-overview.txt -- per-namespace + per-stage rollup
                         (STATUS, OOM count, distinct verdict patterns)
  * _oom-findings.csv -- one row per OOMKilled container across the cluster

Stage is inferred from the path layout the loop creates:

    <input-dir>/by-stage/<stage>/<ns>/report.json

so the aggregator can run stand-alone on any existing oom-loop directory
without re-parsing namespace names.

Usage:
    python3 aggregate-oom.py --input-dir reports/oom-loop-<ts>/
"""

import argparse
import csv
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover(root):
    """Findet alle Per-Namespace-Reports unter <root>/by-stage/<stage>/<ns>/.

    fetch-cluster-oom-loop.sh legt fuer jeden Namespace mit OOMKills eine
    report.json an. Diese Funktion durchlaeuft die Verzeichnis-Struktur
    und gibt pro Namespace ein Tupel zurueck:

        (stage, namespace, path-to-report.json, findings_list)

    findings_list ist eine Liste von Dicts (so wie fetch-cluster-oom.py
    sie via --json schreibt) -- leer, wenn die Datei leer oder defekt
    ist. So koennen weitere Schritte einfach iterieren, ohne sich um
    fehlende Dateien zu kuemmern.

    Stage wird hier aus dem Pfad gelesen (nicht aus dem Namespace-Namen),
    weil der Loop die Stage bereits in die Ordner-Struktur kodiert hat.
    """
    base = root / "by-stage"
    if not base.is_dir():
        return
    for stage_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        stage = stage_dir.name
        for ns_dir in sorted(p for p in stage_dir.iterdir() if p.is_dir()):
            ns = ns_dir.name
            report_path = ns_dir / "report.json"
            findings = []
            if report_path.is_file():
                try:
                    raw = report_path.read_text()
                except OSError as e:
                    print("warning: cannot read {}: {}".format(report_path, e),
                          file=sys.stderr)
                    raw = ""
                # fetch-cluster-oom.py prints nothing when there are zero OOMs in
                # the namespace, so an empty file means "no findings", not a
                # parse error -- treat it as []. A non-empty but malformed
                # file is a real problem and is warned about.
                if raw.strip():
                    try:
                        data = json.loads(raw)
                        if isinstance(data, list):
                            findings = data
                    except json.JSONDecodeError as e:
                        print("warning: cannot parse {}: {}".format(report_path, e),
                              file=sys.stderr)
            yield stage, ns, report_path, findings


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

_PATTERN_PREFIX = re.compile(r"^([A-Z?])\s*-")


def short_pattern(p):
    """Kuerzt einen Verdict-String auf seinen Pattern-Buchstaben.

    Beispiele:
        'A - MEMORY LEAK'    -> 'A'
        'E - STARTUP OVERRUN'-> 'E'
        '? - INDETERMINATE'  -> '?'

    Wird gebraucht, um in der Uebersicht die Patterns kompakt
    aufzuzaehlen (z. B. 'A x2, C x1').
    """
    if not p:
        return "?"
    m = _PATTERN_PREFIX.match(p)
    return m.group(1) if m else p[:1]


def join_patterns(patterns):
    """Zaehlt eine Liste von Pattern-Kuerzeln und formatiert sie kompakt.

    Beispiel: ['A', 'C', 'C']  ->  'A x1, C x2'

    Sortiert alphabetisch, damit derselbe Input immer denselben Output
    erzeugt (wichtig fuer Diff-Vergleiche und stabile Reports).
    """
    if not patterns:
        return "-"
    c = Counter(patterns)
    return ", ".join("{} x{}".format(k, c[k]) for k in sorted(c))


def parse_iso(ts):
    """Parst einen ISO-8601-Zeitstempel zu einem timezone-bewussten datetime.

    Akzeptiert sowohl '...Z' (UTC-Zulu) als auch '+02:00'-Suffixe. Microsekunden
    sind optional. Liefert None bei leerem oder unparsbarem Input -- so kann der
    Aufrufer die Pruefung leicht mit 'if not when' machen.
    """
    if not ts:
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1]
    elif len(s) >= 6 and s[-3] == ":" and s[-6] in "+-":
        s = s[:-6]
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def age(ts):
    """Formatiert das Alter eines Zeitstempels als kompakten String.

    Beispiele:
        '45s'  (Sekunden)
        '12m'  (Minuten)
        '3h'   (Stunden)
        '2d'   (Tage)

    Praktisch fuer die CSV-Spalte 'age' im Findings-Report.
    """
    when = parse_iso(ts)
    if not when:
        return "?"
    s = int((datetime.now(timezone.utc) - when).total_seconds())
    if s < 60:
        return "{}s".format(s)
    if s < 3600:
        return "{}m".format(s // 60)
    if s < 86400:
        return "{}h".format(s // 3600)
    return "{}d".format(s // 86400)


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

_FINDINGS_FIELDS = [
    "stage", "namespace", "pod", "container",
    "node", "workload",
    "oom_at", "age", "lifetime_s",
    "restart_count", "exit_code",
    "pattern_short", "pattern",
    "evidence",
]


def write_findings_csv(path, rows):
    """Schreibt eine Zeile pro OOMKilled-Container in eine CSV-Datei.

    Spalten siehe _FINDINGS_FIELDS oben -- diese Reihenfolge wird strikt
    eingehalten, damit Spreadsheet-Filter und Pivot-Tabellen stabil
    bleiben.
    """
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_FINDINGS_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


_OVR_HEADERS = ("STAGE", "NAMESPACE", "STATUS", "OOMS", "PATTERNS")
_OVR_FMT = "{:<8} {:<40} {:<8} {:>5}  {}"


def render_overview(per_ns_rows, root, prom_args_hint=None):
    """Baut den Text-Inhalt fuer _oom-overview.txt zusammen.

    Aufbau:
      - Kopf mit Quelle, Anzahl Namespaces, kurzer Legende
      - Tabelle pro Namespace (STAGE / NAMESPACE / STATUS / OOMS / PATTERNS)
      - Per-Stage-Rollup am Ende (ein Block pro Stage mit Summen)

    Wird nur aufgerufen, wenn ueberhaupt OOMs gefunden wurden; der Aufrufer
    schreibt das Ergebnis selbst in die Datei.
    """
    header = _OVR_FMT.format(*_OVR_HEADERS)
    sep = "-" * max(len(header), 78)

    lines = [
        "=" * max(len(header), 78),
        "OOM overview - fetch-cluster-oom-loop",
        "Source: {}".format(root),
        "Total namespaces: {}".format(len(per_ns_rows)),
        "Patterns: A=leak  B=spike  C=under-provisioned  D=node-pressure  "
        "E=startup-overrun  ?=indeterminate",
    ]
    if prom_args_hint:
        lines.append("Prometheus: {}".format(prom_args_hint))
    lines += [
        "=" * max(len(header), 78),
        "",
        header,
        sep,
    ]
    for r in per_ns_rows:
        lines.append(_OVR_FMT.format(
            r["stage"], r["namespace"], r["status"],
            r["ooms"], r["patterns_str"],
        ))

    # Per-stage rollup
    lines += ["", sep, "Per-stage totals:", sep]
    stage_totals = {}
    for r in per_ns_rows:
        s = stage_totals.setdefault(
            r["stage"], {"ns": 0, "ooms": 0, "patterns": []})
        s["ns"] += 1
        if isinstance(r["ooms"], int):
            s["ooms"] += r["ooms"]
        s["patterns"].extend(r["patterns_raw"])

    stage_fmt = "{:<8} {:>4} {:>5}  {}"
    lines.append(stage_fmt.format("STAGE", "NS", "OOMS", "PATTERNS"))
    lines.append("-" * 78)
    for stage in sorted(stage_totals):
        s = stage_totals[stage]
        lines.append(stage_fmt.format(
            stage, s["ns"], s["ooms"], join_patterns(s["patterns"]),
        ))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    """Einstiegspunkt des Aggregators.

    Liest den per --input-dir uebergebenen Reportordner, sammelt alle
    Per-Namespace-Findings und schreibt zwei Dateien an den Root des
    Ordners:

      _oom-overview.txt    menschenlesbare Uebersicht
      _oom-findings.csv    eine Zeile pro OOMKilled-Container

    Rueckgabewerte:
      0  alles geschrieben
      1  keine Reports gefunden
      2  --input-dir existiert nicht
    """
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input-dir", required=True,
                    help="oom-loop-<ts>/ directory produced by "
                         "fetch-cluster-oom-loop.sh")
    args = ap.parse_args()

    root = Path(args.input_dir)
    if not root.is_dir():
        print("error: --input-dir does not exist: {}".format(root),
              file=sys.stderr)
        return 2

    per_ns_rows = []
    findings_rows = []
    saw_any = False

    for stage, ns, report_path, findings in discover(root):
        saw_any = True
        if not report_path.is_file():
            per_ns_rows.append({
                "stage": stage,
                "namespace": ns,
                "status": "MISS",
                "ooms": "?",
                "patterns_raw": [],
                "patterns_str": "-",
            })
            continue

        patterns_raw = []
        for f in findings:
            t = (f.get("target") or {})
            v = (f.get("verdict") or {})
            pattern = v.get("pattern") or "?"
            short = short_pattern(pattern)
            patterns_raw.append(short)

            kind = t.get("workload_kind") or ""
            wname = t.get("workload_name") or ""
            workload = "{}/{}".format(kind, wname) if (kind and wname) else (kind or wname or "-")

            started = parse_iso(t.get("started_at"))
            finished = parse_iso(t.get("finished_at"))
            lifetime_s = (int((finished - started).total_seconds())
                          if (started and finished) else "")

            evidence = "; ".join(v.get("evidence") or [])
            findings_rows.append({
                "stage": stage,
                "namespace": ns,
                "pod": f.get("pod") or t.get("pod") or "",
                "container": f.get("container") or t.get("container") or "",
                "node": t.get("node") or "",
                "workload": workload,
                "oom_at": t.get("finished_at") or "",
                "age": age(t.get("finished_at")),
                "lifetime_s": lifetime_s,
                "restart_count": t.get("restart_count") or 0,
                "exit_code": t.get("exit_code") or "",
                "pattern_short": short,
                "pattern": pattern,
                "evidence": evidence,
            })

        per_ns_rows.append({
            "stage": stage,
            "namespace": ns,
            "status": "ok",
            "ooms": len(findings),
            "patterns_raw": patterns_raw,
            "patterns_str": join_patterns(patterns_raw),
        })

    if not saw_any:
        print("error: no per-namespace reports found under {}/by-stage/"
              .format(root), file=sys.stderr)
        return 1

    per_ns_rows.sort(key=lambda r: (r["stage"], r["namespace"]))
    findings_rows.sort(key=lambda r: (r["stage"], r["namespace"],
                                      r["oom_at"], r["pod"]))

    overview_path = root / "_oom-overview.txt"
    csv_path = root / "_oom-findings.csv"

    overview_path.write_text(render_overview(per_ns_rows, root))
    write_findings_csv(csv_path, findings_rows)

    print("wrote {} ({} namespaces)".format(overview_path, len(per_ns_rows)))
    print("wrote {} ({} findings)".format(csv_path, len(findings_rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
