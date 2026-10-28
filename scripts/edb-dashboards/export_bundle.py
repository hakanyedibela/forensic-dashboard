"""Build the customer export from the generated dashboards and the alert rules.

  python3 scripts/edb-dashboards/export_bundle.py <repo-root>

Writes <repo-root>/export/:
  edb-grafana-bundle.json            all dashboards + alert rule groups (input for import-to-grafana.sh)
  dashboards/<name>.json             one file per dashboard for Dashboards -> New -> Import
  alerts/edb-postgres-alerts.rules.yaml   plain Prometheus rule file for
                                     Alerting -> Import alert rules -> Prometheus YAML file
"""
import copy
import datetime
import json
import os
import sys

import yaml

DASHBOARDS = ["dashboard-edb.json", "dashboard-edb-backups.json", "dashboard-edb-alerts.json"]
FOLDER = {"uid": "edb-postgres", "title": "EDB PostgreSQL"}


def import_ready(d):
    """Strip instance-specific fields so Grafana treats it as a new dashboard (uid kept: links use it)."""
    d = copy.deepcopy(d)
    d["id"] = None
    d.pop("version", None)
    for v in d["templating"]["list"]:
        if v["type"] != "custom":
            v["current"] = {}
            if v["type"] == "query":
                v["options"] = []
    return d


def main(root):
    out = os.path.join(root, "export")
    os.makedirs(os.path.join(out, "dashboards"), exist_ok=True)
    os.makedirs(os.path.join(out, "alerts"), exist_ok=True)

    dashboards = []
    for name in DASHBOARDS:
        d = import_ready(json.load(open(os.path.join(root, "dashboards", name))))
        dashboards.append(d)
        with open(os.path.join(out, "dashboards", name), "w") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
            f.write("\n")

    groups = yaml.safe_load(open(os.path.join(root, "manifests", "edb-postgres-alerts.yaml")))["spec"]["groups"]
    with open(os.path.join(out, "alerts", "edb-postgres-alerts.rules.yaml"), "w") as f:
        f.write("# Prometheus rule file. Grafana: Alerting -> Import alert rules -> Prometheus YAML file.\n"
                "# Plain Prometheus: add to rule_files. Kubernetes: use manifests/edb-postgres-alerts.yaml instead.\n")
        yaml.safe_dump({"groups": groups}, f, sort_keys=False, allow_unicode=True, width=200)

    bundle = {
        "format": "edb-grafana-bundle/v1",
        "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "folder": FOLDER,
        "dashboards": dashboards,
        "alertRuleGroups": groups,
    }
    with open(os.path.join(out, "edb-grafana-bundle.json"), "w") as f:
        json.dump(bundle, f, indent=2, ensure_ascii=False)
        f.write("\n")
    rules = sum(len(g["rules"]) for g in groups)
    print(f"export/: {len(dashboards)} dashboards, {len(groups)} rule groups, {rules} alert rules")


if __name__ == "__main__":
    main(sys.argv[1])
