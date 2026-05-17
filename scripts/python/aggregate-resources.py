#!/usr/bin/env python3
"""
aggregate-resources.py

Reads every snapshot.json under a state-loop output directory and produces
a cluster-wide resource overview:

  * _resources-overview.txt  -- text table, per namespace + per-stage totals
  * _resources-overview.csv  -- raw numbers (millicores, MiB, GiB), one row
                                per namespace

The text table is sized to fit comfortably in ~120 columns and uses
human-readable units (m / Mi / Gi). The CSV keeps raw scalar values so it
can be pivoted in a spreadsheet.

Per-namespace columns:

    STAGE       stage as detected by fetch-cluster-state.py
    NAMESPACE   namespace name
    PODS        sum of replicas across Deployments + StatefulSets
    WL          "<num deployments>/<num statefulsets>"
    CPU_REQ     sum of (replicas * cpu_req_millis) across workloads
    CPU_LIM     same for cpu_lim_millis
    MEM_REQ     sum of (replicas * mem_req_mib)
    MEM_LIM     same for mem_lim_mib
    PVCS        number of PVCs
    STOR_GiB    sum of PVC storage in GiB
    QUOTAS      number of ResourceQuotas
    LR          number of LimitRanges
    QMAX%       highest quota-usage percentage across all quota dimensions

Usage:
    python3 aggregate-resources.py --input-dir reports/state-loop-<ts>/
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Unit parsing -- duplicated from fetch-cluster-state.py to keep this
# aggregator stand-alone (no shared package).
# ---------------------------------------------------------------------------

def parse_cpu_millis(v):
    """Wandelt eine Kubernetes-CPU-Angabe in Millicores (int) um.

    Akzeptiert beide Schreibweisen:
      "500m"     -> 500
      "0.5"      -> 500
      "1"        -> 1000

    Liefert 0 fuer leere/None/ungueltige Werte. So koennen Summen ueber
    viele Container/Pods einfach mit '+' gebildet werden, ohne Sonderfaelle.
    """
    if v in (None, "", "<none>"):
        return 0
    s = str(v).strip()
    if s.endswith("m"):
        try:
            return int(float(s[:-1]))
        except ValueError:
            return 0
    try:
        return int(float(s) * 1000)
    except ValueError:
        return 0


_MEM_UNITS = {
    "Ki": 1 / 1024,
    "Mi": 1,
    "Gi": 1024,
    "Ti": 1024 * 1024,
    "K":  1000 / (1024 * 1024),
    "M":  1000 ** 2 / (1024 * 1024),
    "G":  1000 ** 3 / (1024 * 1024),
    "T":  1000 ** 4 / (1024 * 1024),
}


def parse_mem_mib(v):
    """Wandelt eine Kubernetes-Memory-Angabe in MiB (int) um.

    Versteht binaere Suffixe (Ki/Mi/Gi/Ti) und dezimale (K/M/G/T)
    sowie die Bytes-only-Schreibweise ohne Suffix. Alles wird intern auf
    MiB normalisiert, damit summiert werden kann.

    Beispiele:
      "512Mi" -> 512
      "1Gi"   -> 1024
      "500M"  -> 476 (1 MB = 10^6 Byte, 1 MiB = 2^20 Byte)
      "100"   -> 0   (100 Byte sind weniger als 1 MiB)

    Liefert 0 bei leerem/None/ungueltigem Input.
    """
    if v in (None, "", "<none>"):
        return 0
    s = str(v).strip()
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)([A-Za-z]+)?$", s)
    if not m:
        return 0
    n = float(m.group(1))
    unit = m.group(2) or ""
    if unit == "":
        factor = 1 / (1024 * 1024)
    else:
        factor = _MEM_UNITS.get(unit, 0)
    return int(n * factor)


def quota_amount(dim, val):
    """Normalisiert einen ResourceQuota-Wert auf eine vergleichbare Zahl.

    Eine Quota hat unterschiedliche Dimensionen mit unterschiedlichen
    Einheiten:
      - 'requests.cpu' / 'limits.cpu' -> Millicores
      - 'requests.memory' / 'storage' -> MiB
      - 'pods', 'services', 'count/*' -> Stueckzahl (float)

    Diese Funktion erkennt anhand des Namens (`dim`), welche Einheit
    gemeint ist, und liefert immer einen float zurueck -- so kann
    'used / hard' als Prozentsatz berechnet werden.
    """
    if val in (None, ""):
        return 0.0
    d = dim.lower()
    if "cpu" in d:
        return float(parse_cpu_millis(val))
    if "memory" in d or "storage" in d:
        return float(parse_mem_mib(val))
    try:
        return float(str(val))
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def fmt_cpu(millis):
    """Formatiert CPU in Millicores menschenfreundlich.

    < 1000 Millicores: '200m'
    >= 1000:           '1.5' oder '2' (Cores, ohne 'm')

    Wird in der Tabellen-Ausgabe verwendet, damit grosse Werte nicht
    'kilo-millicores' wirken (z. B. '4000m' lieber als '4').
    """
    if millis is None:
        return "-"
    if millis >= 1000:
        cores = millis / 1000.0
        if abs(cores - round(cores)) < 0.05:
            return "{:.0f}".format(cores)
        return "{:.2f}".format(cores)
    return "{}m".format(int(millis))


def fmt_mem(mib):
    """Formatiert Memory in MiB menschenfreundlich.

    < 1024 MiB: '512Mi'
    >= 1024:    '1.5Gi' oder '2Gi'

    Dieselbe Idee wie fmt_cpu: ab GiB-Bereich auf Gi umsteigen.
    """
    if mib is None:
        return "-"
    if mib >= 1024:
        gib = mib / 1024.0
        if abs(gib - round(gib)) < 0.05:
            return "{:.0f}Gi".format(gib)
        return "{:.1f}Gi".format(gib)
    return "{}Mi".format(int(mib))


def fmt_storage(gib):
    """Formatiert PVC-Storage in GiB.

    0 wird als '0' angezeigt (statt '0Gi'), damit leere Namespaces in
    der Tabelle ruhiger wirken.
    """
    if gib is None:
        return "-"
    if gib == 0:
        return "0"
    if gib < 10:
        return "{:.1f}Gi".format(gib)
    return "{:.0f}Gi".format(gib)


def fmt_pct(pct):
    """Formatiert einen Prozentsatz ohne Nachkommastellen: '85%'.

    Liefert '-' bei None (z. B. wenn keine Quota existiert).
    """
    if pct is None:
        return "-"
    return "{:.0f}%".format(pct)


# ---------------------------------------------------------------------------
# Per-namespace rollup
# ---------------------------------------------------------------------------

def rollup_namespace(snap):
    """Aggregiert einen snapshot.json zu einer Zeile fuer die Resources-Uebersicht.

    Berechnet pro Namespace:
      pods           Summe der replicas ueber alle Deployments+StatefulSets
      cpu_req/lim    Summe (replicas * container_request_or_limit) -- das
                     ist der tatsaechliche Cluster-Footprint, nicht der
                     Per-Pod-Wert.
      mem_req/lim    Dasselbe fuer Memory in MiB
      storage_gib    Summe ueber alle PVC-Groessen
      qmax_pct       Hoechster used/hard-Anteil ueber alle Quota-Dimensionen
                     (zeigt, wie nah der Namespace am Quota-Limit ist)
      counts         Anzahl PVCs, Quotas, LimitRanges, Services, NetPols

    Zurueckgabe: ein dict mit allen Spalten, die der Textreport und die
    CSV brauchen.
    """
    deployments = snap.get("deployments") or []
    statefulsets = snap.get("statefulsets") or []
    pvcs = snap.get("pvcs") or []
    quotas = snap.get("resourceQuotas") or []
    limit_ranges = snap.get("limitRanges") or []
    services = snap.get("services") or []
    netpols = snap.get("networkPolicies") or []

    pods = 0
    cpu_req = cpu_lim = mem_req = mem_lim = 0
    for w in deployments + statefulsets:
        r = w.get("replicas") or 0
        pods += r
        cpu_req += r * (w.get("cpu_req_millis") or 0)
        cpu_lim += r * (w.get("cpu_lim_millis") or 0)
        mem_req += r * (w.get("mem_req_mib") or 0)
        mem_lim += r * (w.get("mem_lim_mib") or 0)

    storage_gib = 0.0
    for p in pvcs:
        storage_gib += float(p.get("storage_gib") or 0)

    # Highest used/hard percentage across all quota dimensions
    qmax_pct = None
    for q in quotas:
        hard = q.get("hard") or {}
        used = q.get("used") or {}
        for dim in sorted(set(hard.keys()) | set(used.keys())):
            h = quota_amount(dim, hard.get(dim))
            u = quota_amount(dim, used.get(dim))
            if h > 0:
                pct = (u / h) * 100.0
                if qmax_pct is None or pct > qmax_pct:
                    qmax_pct = pct

    return {
        "stage": snap.get("stage") or "other",
        "namespace": snap.get("namespace") or "",
        "pods": pods,
        "deployments": len(deployments),
        "statefulsets": len(statefulsets),
        "cpu_req_m": cpu_req,
        "cpu_lim_m": cpu_lim,
        "mem_req_mib": mem_req,
        "mem_lim_mib": mem_lim,
        "pvcs": len(pvcs),
        "storage_gib": round(storage_gib, 2),
        "quotas": len(quotas),
        "limit_ranges": len(limit_ranges),
        "services": len(services),
        "netpols": len(netpols),
        "qmax_pct": qmax_pct,
    }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_snapshots(root):
    """Sammelt alle snapshot.json-Dateien unter <root>.

    Der bash-Loop legt die Snapshots im verschachtelten Pfad
    'by-stage/<stage>/<ns>/by-stage/<stage>/<ns>/snapshot.json' ab
    (Layout-Detail von fetch-cluster-state.py + dem Loop-Wrapper).
    Damit nicht versehentlich dieselbe Datei doppelt gezaehlt wird,
    wird auf (stage, namespace) dedupliziert.

    Bei mehreren Treffern fuer denselben Namespace wird der mit dem
    tiefsten Pfad bevorzugt -- das ist der, den Python tatsaechlich
    geschrieben hat. Defensiv: in der Praxis liefert die Suche meistens
    nur einen Treffer pro Namespace.

    Rueckgabe: Liste von Snapshot-Dicts (geparsed aus JSON).
    """
    found = {}
    for path in sorted(root.rglob("snapshot.json")):
        try:
            snap = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print("warning: cannot read {}: {}".format(path, e),
                  file=sys.stderr)
            continue
        key = (snap.get("stage") or "other", snap.get("namespace") or "")
        if not key[1]:
            continue
        # Prefer the snapshot at the greater path depth (i.e. the inner one
        # if the loop double-nests). They are identical in content, this is
        # just defensive.
        prev = found.get(key)
        if prev is None or len(path.parts) > len(prev[0].parts):
            found[key] = (path, snap)
    return [snap for _, snap in found.values()]


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

_CSV_FIELDS = [
    "stage", "namespace",
    "pods", "deployments", "statefulsets",
    "cpu_req_millis", "cpu_lim_millis",
    "mem_req_mib", "mem_lim_mib",
    "pvcs", "storage_gib",
    "quotas", "limit_ranges",
    "services", "netpols",
    "quota_max_pct",
]


def write_csv(path, rows):
    """Schreibt eine Zeile pro Namespace mit Roh-Zahlen in eine CSV.

    Im Unterschied zur Textausgabe werden hier KEINE Einheiten formatiert:
    CPU bleibt in Millicores, Memory in MiB, Storage in GiB. So kann das
    File direkt in Excel/Google Sheets gepivoted werden, ohne erst Strings
    wie '1.5Gi' parsen zu muessen.
    """
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({
                "stage": r["stage"],
                "namespace": r["namespace"],
                "pods": r["pods"],
                "deployments": r["deployments"],
                "statefulsets": r["statefulsets"],
                "cpu_req_millis": r["cpu_req_m"],
                "cpu_lim_millis": r["cpu_lim_m"],
                "mem_req_mib": r["mem_req_mib"],
                "mem_lim_mib": r["mem_lim_mib"],
                "pvcs": r["pvcs"],
                "storage_gib": r["storage_gib"],
                "quotas": r["quotas"],
                "limit_ranges": r["limit_ranges"],
                "services": r["services"],
                "netpols": r["netpols"],
                "quota_max_pct": (
                    "" if r["qmax_pct"] is None
                    else round(r["qmax_pct"], 1)
                ),
            })


_HEAD_FMT = ("{:<8} {:<38} {:>4} {:>5} "
             "{:>8} {:>8} {:>8} {:>8} "
             "{:>4} {:>8} {:>6} {:>3} "
             "{:>4} {:>3} {:>5}")

_HEADERS = ("STAGE", "NAMESPACE", "PODS", "WL",
            "CPU_REQ", "CPU_LIM", "MEM_REQ", "MEM_LIM",
            "PVCS", "STOR", "QUOTAS", "LR",
            "SVCS", "NP", "QMAX%")


def render_row(r):
    """Formatiert ein Namespace-Dict (aus rollup_namespace) als Tabellen-Zeile.

    Verwendet _HEAD_FMT mit festen Spaltenbreiten, damit Header und
    Daten sauber untereinander stehen. Einheiten kommen ueber die
    fmt_*-Helper.
    """
    return _HEAD_FMT.format(
        r["stage"], r["namespace"],
        r["pods"],
        "{}/{}".format(r["deployments"], r["statefulsets"]),
        fmt_cpu(r["cpu_req_m"]),
        fmt_cpu(r["cpu_lim_m"]),
        fmt_mem(r["mem_req_mib"]),
        fmt_mem(r["mem_lim_mib"]),
        r["pvcs"],
        fmt_storage(r["storage_gib"]),
        r["quotas"],
        r["limit_ranges"],
        r["services"],
        r["netpols"],
        fmt_pct(r["qmax_pct"]),
    )


def render_stage_totals(rows):
    """Summiert pro Stage und liefert den Text-Block fuer 'Per-stage totals'.

    Gleiche Spalten wie die Hauptzeile, aber:
      - keine NAMESPACE-Spalte (stattdessen 'NS' = Anzahl Namespaces)
      - kein QMAX% (Quota-Maximum laesst sich ueber mehrere Namespaces
        nicht sinnvoll summieren)

    Sortiert nach Stage-Name, damit Diffs zwischen zwei Runs stabil sind.
    """
    fmt = ("{:<8} {:>4} {:>4} {:>5} "
           "{:>8} {:>8} {:>8} {:>8} "
           "{:>4} {:>8} {:>6} {:>3} "
           "{:>4} {:>3}")
    header = fmt.format(
        "STAGE", "NS", "PODS", "WL",
        "CPU_REQ", "CPU_LIM", "MEM_REQ", "MEM_LIM",
        "PVCS", "STOR", "QUOTAS", "LR",
        "SVCS", "NP",
    )

    totals_by_stage = {}
    for r in rows:
        s = totals_by_stage.setdefault(r["stage"], {
            "ns": 0, "pods": 0,
            "deployments": 0, "statefulsets": 0,
            "cpu_req_m": 0, "cpu_lim_m": 0,
            "mem_req_mib": 0, "mem_lim_mib": 0,
            "pvcs": 0, "storage_gib": 0.0,
            "quotas": 0, "limit_ranges": 0,
            "services": 0, "netpols": 0,
        })
        s["ns"] += 1
        for k in ("pods", "deployments", "statefulsets",
                  "cpu_req_m", "cpu_lim_m",
                  "mem_req_mib", "mem_lim_mib",
                  "pvcs", "quotas", "limit_ranges",
                  "services", "netpols"):
            s[k] += r[k]
        s["storage_gib"] += r["storage_gib"]

    lines = [header, "-" * len(header)]
    for stage in sorted(totals_by_stage):
        s = totals_by_stage[stage]
        lines.append(fmt.format(
            stage, s["ns"], s["pods"],
            "{}/{}".format(s["deployments"], s["statefulsets"]),
            fmt_cpu(s["cpu_req_m"]),
            fmt_cpu(s["cpu_lim_m"]),
            fmt_mem(s["mem_req_mib"]),
            fmt_mem(s["mem_lim_mib"]),
            s["pvcs"],
            fmt_storage(s["storage_gib"]),
            s["quotas"], s["limit_ranges"],
            s["services"], s["netpols"],
        ))
    return "\n".join(lines)


def write_text(path, rows, input_dir):
    """Schreibt die menschenlesbare _resources-overview.txt.

    Aufbau:
      - Kopf (Quelle, Anzahl Namespaces, Legende fuer Einheiten/Spalten)
      - Tabelle pro Namespace (sortiert nach stage, dann namespace)
      - Per-Stage-Rollup unten

    Sortierung sorgt fuer stabile Outputs -- nuetzlich, wenn man zwei
    Runs gegeneinander diffen will.
    """
    rows_sorted = sorted(rows, key=lambda r: (r["stage"], r["namespace"]))
    header = _HEAD_FMT.format(*_HEADERS)
    sep = "-" * len(header)

    lines = [
        "=" * len(header),
        "Resources overview - fetch-cluster-state loop",
        "Source: {}".format(input_dir),
        "Total namespaces: {}".format(len(rows_sorted)),
        "Units: CPU in millicores ('m') or cores, memory in MiB/GiB, storage in GiB.",
        "QMAX% = highest used/hard ratio across any ResourceQuota dimension.",
        "WL    = <deployments>/<statefulsets> count.",
        "=" * len(header),
        "",
        header,
        sep,
    ]
    lines.extend(render_row(r) for r in rows_sorted)
    lines.append("")
    lines.append(sep)
    lines.append("Per-stage totals:")
    lines.append(sep)
    lines.append(render_stage_totals(rows_sorted))
    lines.append("")

    path.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    """Einstiegspunkt des Aggregators.

    Liest --input-dir, ruft find_snapshots() + rollup_namespace() pro
    Namespace auf und schreibt zwei Dateien an den Root des Ordners:

      _resources-overview.txt  menschenlesbare Tabelle + Stage-Totals
      _resources-overview.csv  Rohwerte (Millicores, MiB, GiB) zum Pivoten

    Rueckgabewerte:
      0  alles geschrieben
      1  keine Snapshots gefunden
      2  --input-dir existiert nicht
    """
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input-dir", required=True,
                    help="state-loop-<ts>/ directory produced by "
                         "fetch-cluster-state-loop.sh")
    ap.add_argument("--text-name", default="_resources-overview.txt",
                    help="filename for the text report")
    ap.add_argument("--csv-name", default="_resources-overview.csv",
                    help="filename for the CSV report")
    args = ap.parse_args()

    root = Path(args.input_dir)
    if not root.is_dir():
        print("error: --input-dir does not exist: {}".format(root),
              file=sys.stderr)
        return 2

    snapshots = find_snapshots(root)
    if not snapshots:
        print("error: no snapshot.json files found under {}".format(root),
              file=sys.stderr)
        return 1

    rows = [rollup_namespace(s) for s in snapshots]
    rows.sort(key=lambda r: (r["stage"], r["namespace"]))

    text_path = root / args.text_name
    csv_path = root / args.csv_name
    write_text(text_path, rows, root)
    write_csv(csv_path, rows)

    print("wrote {} ({} namespaces)".format(text_path, len(rows)))
    print("wrote {}".format(csv_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
