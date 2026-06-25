#!/usr/bin/env python3
"""
reconcile-state.py

Reconcile the *current* cluster state (snapshot.json) against the *desired*
manifests (desired/*.yaml) for every namespace under a state-loop output
directory produced by fetch-cluster-state.py, and write one presence-level
drift CSV per stage:

  * _reconcile-<stage>.csv  -- one row per (kind, name) per namespace, with
                               in_current / in_desired flags and a status
                               (IN_SYNC / MISSING_IN_CLUSTER / NOT_DESIRED)

Matching is presence-level: resources are keyed by (kind, name). Field values
are NOT compared. The report mainly validates that the desired manifests
faithfully cover what the cluster actually has.

Usage:
    python3 reconcile-state.py --input-dir reports/state-loop-<ts>/

Requires:
    * Python 3.6.8+
    * PyYAML (optional; falls back to a kind/name line extractor if missing)
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

try:
    import yaml  # optional
except ImportError:  # pragma: no cover
    yaml = None


# Kinds we reconcile, in stable CSV sort-friendly order. Maps the snapshot.json
# array key -> Kubernetes kind. The Namespace object itself is handled
# separately (it has no array; the snapshot *is* the namespace).
SNAPSHOT_KEY_TO_KIND = {
    "deployments": "Deployment",
    "statefulsets": "StatefulSet",
    "services": "Service",
    "hpas": "HorizontalPodAutoscaler",
    "pvcs": "PersistentVolumeClaim",
    "resourceQuotas": "ResourceQuota",
    "limitRanges": "LimitRange",
    "networkPolicies": "NetworkPolicy",
}

CSV_HEADER = [
    "stage",
    "namespace",
    "kind",
    "name",
    "in_current",
    "in_desired",
    "status",
]


def current_resources(snapshot):
    """Liefert die Menge der aktuellen Ressourcen als {(kind, name)}.

    Die Namespace-Ressource selbst wird immer aufgenommen; danach jede
    benannte Ressource aus den bekannten Snapshot-Arrays. Fehlende oder leere
    Arrays tragen nichts bei. Doppelte (kind, name) werden durch das Set
    automatisch zusammengefasst.
    """
    found = set()
    ns = snapshot.get("namespace")
    if ns:
        found.add(("Namespace", ns))
    for key, kind in SNAPSHOT_KEY_TO_KIND.items():
        for item in snapshot.get(key) or []:
            name = item.get("name")
            if name:
                found.add((kind, name))
    return found


def desired_from_yaml_text(text):
    """Liefert {(kind, name)} aus einem (Multi-Dokument-)YAML-Text.

    Nutzt PyYAML, wenn verfuegbar (Produktionspfad). Andernfalls greift ein
    Zeilen-Extraktor: pro Dokument das top-level 'kind:' und das 'name:'
    direkt unter dem top-level 'metadata:'-Block. Dieser Fallback setzt die
    stabile Formatierung der von fetch-cluster-state.py erzeugten Manifeste
    voraus: block-style 'metadata:' (kein inline '{}') und keine
    ownerReferences vor metadata.name. PyYAML behandelt beide Faelle korrekt.
    """
    found = set()
    if yaml is not None:
        for doc in yaml.safe_load_all(text):
            if not isinstance(doc, dict):
                continue
            kind = doc.get("kind")
            name = (doc.get("metadata") or {}).get("name")
            if kind and name:
                found.add((kind, name))
        return found

    # Fallback: split on document markers, scan lines.
    #
    # We must read the TOP-LEVEL `kind:` and the `name:` that lives directly
    # under the TOP-LEVEL `metadata:` block. A naive "first name: anywhere"
    # grabs the wrong key (e.g. spec.podSelector.matchLabels.name or
    # scaleTargetRef.name) when spec: precedes metadata: in a manually-authored
    # manifest. So:
    #   * kind: must be at column 0 (matched against the raw line).
    #   * name: must be a direct child of a top-level `metadata:` key, i.e. we
    #     only accept a `name:` while we are inside the metadata block (a
    #     top-level `metadata:` was seen and we have not yet dedented back to
    #     another top-level key).
    for chunk in re.split(r"^---\s*$", text, flags=re.MULTILINE):
        kind = None
        name = None
        in_metadata = False
        for line in chunk.splitlines():
            if not line.strip():
                continue
            top_level = re.match(r"^\S", line)
            if kind is None and re.match(r"^kind:\s*\S", line):
                kind = line[len("kind:"):].strip()
                in_metadata = False
                continue
            if re.match(r"^metadata:\s*$", line):
                in_metadata = True
                continue
            if top_level:
                # any other top-level key ends the metadata block
                in_metadata = False
                continue
            if in_metadata and name is None and re.match(r"^\s+name:\s*\S", line):
                name = line.split("name:", 1)[1].strip()
        if kind and name:
            found.add((kind, name))
    return found


def read_namespace(ns_dir):
    """Liest ein Namespace-Verzeichnis und liefert (current, desired) als Sets.

    current stammt aus snapshot.json, desired aus allen desired/*.yaml-Dateien.
    Fehlt das desired/-Verzeichnis, ist desired leer (alle Ressourcen gelten
    dann als NOT_DESIRED).
    """
    snapshot_path = ns_dir / "snapshot.json"
    snapshot = json.loads(snapshot_path.read_text())
    current = current_resources(snapshot)

    desired = set()
    desired_dir = ns_dir / "desired"
    if desired_dir.is_dir():
        for path in sorted(desired_dir.glob("*.yaml")):
            desired |= desired_from_yaml_text(path.read_text())
    return current, desired


def reconcile_rows(stage, namespace, current, desired):
    """Vergleicht current/desired (presence-level) und liefert CSV-Zeilen.

    Eine Zeile je (kind, name) aus der Vereinigung beider Mengen, sortiert
    nach (kind, name) fuer stabile Diffs.
    """
    rows = []
    for kind, name in sorted(current | desired):
        in_cur = (kind, name) in current
        in_des = (kind, name) in desired
        if in_cur and in_des:
            status = "IN_SYNC"
        elif in_des:
            status = "MISSING_IN_CLUSTER"
        else:
            status = "NOT_DESIRED"
        rows.append({
            "stage": stage,
            "namespace": namespace,
            "kind": kind,
            "name": name,
            "in_current": str(in_cur),
            "in_desired": str(in_des),
            "status": status,
        })
    return rows


def write_csv(path, rows):
    """Schreibt CSV_HEADER + rows nach path."""
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)


def find_snapshots(root):
    """Sammelt alle snapshot.json-Pfade unter <root>, dedupliziert auf
    (stage, namespace).

    Der Loop-Wrapper um fetch-cluster-state.py legt die Snapshots im
    verschachtelten Pfad
    'by-stage/<stage>/<ns>/by-stage/<stage>/<ns>/snapshot.json' ab. Stage und
    Namespace werden aus dem JSON-Inhalt abgeleitet, nicht aus den
    Verzeichnisnamen. Snapshots ohne Namespace werden uebersprungen.

    Bei mehreren Treffern fuer denselben (stage, namespace) wird der mit dem
    tiefsten Pfad bevorzugt -- das ist der, den Python tatsaechlich geschrieben
    hat. Defensiv: in der Praxis sind die Treffer inhaltlich identisch.

    Rueckgabe: Liste von (stage, namespace, snapshot_path)-Tupeln.
    """
    found = {}
    for path in sorted(root.rglob("snapshot.json")):
        try:
            snap = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            sys.stderr.write("warning: cannot read %s: %s\n" % (path, e))
            continue
        stage = snap.get("stage") or "other"
        namespace = snap.get("namespace")
        if not namespace:
            continue
        key = (stage, namespace)
        prev = found.get(key)
        if prev is None or len(path.parts) > len(prev.parts):
            found[key] = path
    return [(stage, namespace, path)
            for (stage, namespace), path in found.items()]


def run(input_dir):
    """Reconciliert alle Namespaces unter input_dir und schreibt je Stage eine
    CSV-Datei in input_dir. Liefert die Liste der geschriebenen Pfade.

    Snapshots werden rekursiv via rglob gefunden; Stage und Namespace stammen
    aus dem snapshot.json-Inhalt (siehe find_snapshots), nicht aus den
    Verzeichnisnamen.
    """
    input_dir = Path(input_dir)
    rows_by_stage = {}

    for stage, namespace, snapshot_path in find_snapshots(input_dir):
        ns_dir = snapshot_path.parent
        current, desired = read_namespace(ns_dir)
        rows = reconcile_rows(stage, namespace, current, desired)
        rows_by_stage.setdefault(stage, []).extend(rows)

    written = []
    for stage, rows in sorted(rows_by_stage.items()):
        rows.sort(key=lambda r: (r["namespace"], r["kind"], r["name"]))
        out = input_dir / ("_reconcile-%s.csv" % stage)
        write_csv(out, rows)
        written.append(out)
        n_ns = len({r["namespace"] for r in rows})
        n_out = sum(1 for r in rows if r["status"] != "IN_SYNC")
        print("  [%s] %d namespace(s), %d resource(s), %d out-of-sync -> %s"
              % (stage, n_ns, len(rows), n_out, out.name))

    if not written:
        print("No namespaces found under %s" % input_dir)
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Reconcile current vs desired cluster state into per-stage CSVs.")
    parser.add_argument(
        "--input-dir", required=True,
        help="A state-loop-<ts> report directory (snapshots found recursively).")
    args = parser.parse_args(argv)

    input_dir = Path(args.input_dir)
    if not input_dir.is_dir():
        parser.error("input-dir does not exist: %s" % input_dir)

    run(input_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
