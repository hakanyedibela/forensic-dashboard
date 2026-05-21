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
