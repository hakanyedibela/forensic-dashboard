#!/usr/bin/env python3
"""Import edb-grafana-bundle.json (dashboards + alert rules) into a Grafana instance.

Python 3 standard library only. Credentials come from the environment, never from
arguments (arguments are visible in `ps`).

  export GRAFANA_URL=https://grafana.example.org
  export GRAFANA_TOKEN=glsa_...            # service account token, role Editor (alerts: Editor or Admin)
  #   or GRAFANA_USER / GRAFANA_PASSWORD
  python3 import-to-grafana.py [edb-grafana-bundle.json]

Optional:
  PROM_DS_UID=<uid>     Prometheus datasource the alert rules query (default: default Prometheus datasource)
  ALERTS=grafana|none   grafana (default): import the rules as Grafana-managed alert rules.
                        none: dashboards only, e.g. when the rules are deployed as PrometheusRule
                        (importing both evaluates every rule twice).
  ALERTS_PAUSED=1       import alert rules paused (review first, enable in the UI)
  GRAFANA_INSECURE=1    skip TLS verification (self-signed certificates)
"""
import base64
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request


def die(msg):
    sys.exit(f"ERROR: {msg}")


URL = os.environ.get("GRAFANA_URL", "").rstrip("/") or die("GRAFANA_URL is not set")
TOKEN = os.environ.get("GRAFANA_TOKEN")
USER, PASSWORD = os.environ.get("GRAFANA_USER"), os.environ.get("GRAFANA_PASSWORD")
if TOKEN:
    AUTH = f"Bearer {TOKEN}"
elif USER and PASSWORD:
    AUTH = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
else:
    die("set GRAFANA_TOKEN, or GRAFANA_USER and GRAFANA_PASSWORD")
CTX = ssl._create_unverified_context() if os.environ.get("GRAFANA_INSECURE") == "1" else None
ALERTS = os.environ.get("ALERTS", "grafana")
if ALERTS not in ("grafana", "none"):
    die("ALERTS must be 'grafana' or 'none'")


def call(method, path, body=None, content_type="application/json", headers=None):
    data = None
    if body is not None:
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    req = urllib.request.Request(URL + path, data=data, method=method)
    req.add_header("Authorization", AUTH)
    req.add_header("Content-Type", content_type)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"message": raw.decode(errors="replace")[:300]}
    except urllib.error.URLError as e:
        die(f"cannot reach {URL}: {e.reason}")


def yaml_group(group):
    """Serialise one rule group as YAML without PyYAML: JSON is valid YAML."""
    return json.dumps(group)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                             "edb-grafana-bundle.json")
    bundle = json.load(open(path))
    if bundle.get("format") != "edb-grafana-bundle/v1":
        die(f"{path} is not an edb-grafana-bundle/v1 file")

    status, health = call("GET", "/api/health")
    print(f"Grafana {health.get('version', '?')} at {URL}")
    status, me = call("GET", "/api/user")
    if status != 200:
        die(f"authentication failed (HTTP {status}): {me.get('message')}")

    folder = bundle["folder"]
    status, body = call("POST", "/api/folders", folder)
    if status == 200:
        print(f"folder   created: {folder['title']}")
    elif status in (409, 412) or "already exists" in str(body.get("message", "")).lower():
        print(f"folder   exists:  {folder['title']}")
    else:
        die(f"folder: HTTP {status} {body.get('message')}")

    failed = 0
    for d in bundle["dashboards"]:
        status, body = call("POST", "/api/dashboards/db", {
            "dashboard": d, "folderUid": folder["uid"], "overwrite": True,
            "message": f"edb-grafana-bundle {bundle['generated']}"})
        ok = status == 200
        failed += not ok
        print(f"dashboard {'ok' if ok else 'FAILED'}: {d['title']}" + ("" if ok else f" (HTTP {status} {body.get('message')})"))

    if ALERTS == "none":
        print("alerts   skipped (ALERTS=none)")
    else:
        ds_uid = os.environ.get("PROM_DS_UID")
        if not ds_uid:
            status, dss = call("GET", "/api/datasources")
            proms = [x for x in (dss if status == 200 else []) if x.get("type") == "prometheus"]
            if not proms:
                die("no Prometheus datasource found; set PROM_DS_UID")
            ds = next((x for x in proms if x.get("isDefault")), proms[0])
            ds_uid = ds["uid"]
            print(f"alerts   use datasource '{ds['name']}' ({ds_uid}); override with PROM_DS_UID")
        headers = {"X-Grafana-Alerting-Datasource-UID": ds_uid}
        if os.environ.get("ALERTS_PAUSED") == "1":
            headers["X-Grafana-Alerting-Alert-Rules-Paused"] = "true"
        # The convert API files rules under a folder named like the namespace path segment.
        ns = urllib.parse.quote(folder["title"], safe="")
        for g in bundle["alertRuleGroups"]:
            status, body = call("POST", f"/api/convert/prometheus/config/v1/rules/{ns}", yaml_group(g),
                                content_type="application/yaml", headers=headers)
            ok = status in (200, 202)
            failed += not ok
            print(f"alerts   {'ok' if ok else 'FAILED'}: group {g['name']} ({len(g['rules'])} rules)"
                  + ("" if ok else f" (HTTP {status} {body.get('error') or body.get('message')})"))
            if status == 404:
                die("this Grafana has no Prometheus-rule import API (needs Grafana 12+). Import "
                    "alerts/edb-postgres-alerts.rules.yaml via the UI, or deploy the PrometheusRule.")
    if failed:
        sys.exit(f"{failed} step(s) failed")
    print("done")


if __name__ == "__main__":
    main()
