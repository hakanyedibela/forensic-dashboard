#!/usr/bin/env python3
"""Cluster usage report: configured CPU/mem limits & requests vs. real Thanos
usage, rolled up per namespace/workload/pod/container, plus an OOM-killed list
from live pod state + Thanos history. Self-contained (stdlib only); runs locally
via oc/kubectl and in-cluster as a CronJob. See scripts/README.md."""

import argparse
import csv
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone


# ----------------------------------------------------------- quantity helpers

def parse_cpu(s):
    """Kubernetes CPU quantity -> cores (float). '100m'->0.1, '1'->1.0.

    Accepts milli ('m'), micro ('u'), nano ('n') suffixes and plain numbers.
    None/'' -> None.
    """
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = s.strip()
    if not s:
        return None
    try:
        if s.endswith("m"):
            return float(s[:-1]) / 1000
        if s.endswith("u"):
            return float(s[:-1]) / 1_000_000
        if s.endswith("n"):
            return float(s[:-1]) / 1_000_000_000
        return float(s)
    except ValueError:
        return None


def parse_mem(s):
    """Memory quantity -> bytes (int). Binary (Ki/Mi/Gi/Ti/Pi) and decimal
    (K/M/G/T/P) suffixes; plain numbers are bytes. None/invalid -> None."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    units = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
             "Pi": 1024**5, "K": 1000, "M": 10**6, "G": 10**9, "T": 10**12,
             "P": 10**15}
    for suf, mul in units.items():
        if s.endswith(suf):
            try:
                return int(float(s[:-len(suf)]) * mul)
            except ValueError:
                return None
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def fmt_cores(v):
    """Cores -> '0.100' style string. None -> '-'."""
    return "-" if v is None else f"{v:.3f}"


def fmt_bytes(n):
    """Bytes -> short unit string ('1.5Ki', '2.0Gi'). None -> '-'."""
    if n is None:
        return "-"
    v = float(n)
    for unit in ["B", "Ki", "Mi", "Gi", "Ti"]:
        if v < 1024:
            return f"{v:.1f}{unit}"
        v /= 1024
    return f"{v:.1f}Pi"


def fmt_pct(v):
    """Percentage float -> '95.0%'. None -> '-'."""
    return "-" if v is None else f"{v:.1f}%"


# ------------------------------------------------------------ stage detection

STAGE_KEYWORDS = ("ref", "prod", "test", "phase", "pnext")


def detect_stage(ns):
    """Stage from namespace name per convention pid-<id>-<app>-<STAGE>-<num>-...

    Checks dash-segment index 3 first (canonical position), then any segment,
    matching the bash loops' detect_stage. Returns 'other' if no keyword found.
    """
    parts = ns.lower().split("-")
    if len(parts) > 3 and parts[3] in STAGE_KEYWORDS:
        return parts[3]
    for p in parts:
        if p in STAGE_KEYWORDS:
            return p
    return "other"


# --------------------------------------------------- relationship resolution

def workload_for(pod, rs_index):
    """Logical workload (kind, name) for a pod. Follows pod -> ReplicaSet ->
    Deployment via ownerReferences so we report 'Deployment/web' rather than
    the ReplicaSet. rs_index maps (ns, name) -> ReplicaSet object. Returns
    ('', '') for pods with no controller owner."""
    ns = pod["metadata"]["namespace"]
    for owner in pod.get("metadata", {}).get("ownerReferences", []):
        if not owner.get("controller"):
            continue
        kind, name = owner["kind"], owner["name"]
        if kind == "ReplicaSet":
            rs = rs_index.get((ns, name))
            if rs:
                for o2 in rs.get("metadata", {}).get("ownerReferences", []):
                    if o2.get("controller"):
                        return o2.get("kind", "ReplicaSet"), o2.get("name", name)
            return kind, name
        return kind, name
    return "", ""


# ------------------------------------------------------------- rollup math

def sum_limit(values):
    """Sum configured requests/limits. Any unset (None) contributor means the
    aggregate is unbounded -> None. Empty -> None."""
    values = list(values)
    if not values or any(v is None for v in values):
        return None
    return sum(values)


def sum_usage(values):
    """Sum observed usage. None means 'no data' for that contributor and is
    skipped. Returns None only when every contributor is missing."""
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def util_pct(usage, limit):
    """usage/limit as a percentage, or None when usage missing or limit
    unknown/zero."""
    if usage is None or not limit:
        return None
    return usage / limit * 100.0


def main(argv=None):
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main())
