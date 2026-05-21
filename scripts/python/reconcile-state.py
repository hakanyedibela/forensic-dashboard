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

    Nutzt PyYAML, wenn verfuegbar. Andernfalls greift ein einfacher
    Zeilen-Extraktor: pro Dokument das erste 'kind:' und das erste 'name:'.
    Das reicht fuer die von fetch-cluster-state.py erzeugten Manifeste, die
    eine stabile Formatierung haben.
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
    for chunk in re.split(r"^---\s*$", text, flags=re.MULTILINE):
        kind = None
        name = None
        for line in chunk.splitlines():
            stripped = line.strip()
            if kind is None and stripped.startswith("kind:"):
                kind = stripped[len("kind:"):].strip()
            elif name is None and re.match(r"name:\s*\S", stripped):
                name = stripped[len("name:"):].strip()
            if kind and name:
                break
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
