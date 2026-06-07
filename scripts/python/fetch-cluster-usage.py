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


def main(argv=None):
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main())
