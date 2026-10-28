"""Tooltips for the hand-maintained Crunchy dashboards (dashboards/dashboard-crunchy*.json).

  python3 scripts/crunchy-dashboards/apply_tooltips.py <repo-root>          # write tooltips
  python3 scripts/crunchy-dashboards/apply_tooltips.py <repo-root> --check  # fail if any are missing

Sets panel descriptions (the (i) next to the title), variable and link tooltips.
Existing panel descriptions are kept; table columns are appended to the table's
description as a markdown list because Grafana 13 tables do not show field
descriptions in the column header. Overview is English, Backups German, as the panels.
Metrics come from Crunchy pgMonitor (pg_up, ccp_*) and kube-state-metrics.
"""
import json
import os
import sys

OVERVIEW = {
    "file": "dashboard-crunchy.json",
    "dashboard": "Crunchy PostgreSQL (PGO) cluster health, connections, transactions, replication and backups",
    "vars": {
        "datasource": "Prometheus (or Thanos Querier) datasource all panels query.",
        "cluster": "PostgresCluster (label 'pg_cluster' set by the Crunchy pgMonitor exporter).",
    },
    "links": {},
    "panels": {
        "Instances": "Number of PostgreSQL instances reporting pg_up for this cluster.",
        "Ready Instances": "Instances whose exporter can reach PostgreSQL (pg_up = 1). Red below 2 = no standby available.",
        "Cache Hit Ratio": "Share of blocks served from shared_buffers instead of disk, cumulative since the last "
                           "statistics reset (not the current value, so it moves slowly). Red below 80 %, green from 95 %.",
        "Active Backends": "All connections (active and idle) summed over all instances (ccp_connection_stats_total).",
        "Database Size": "Size of all databases on the primary (role=master).",
        "WAL Size": "Current size of the pg_wal directory on the primary. A steady rise usually means archiving "
                    "is failing or a replication slot holds WAL back.",
        "Connections": "Total connections, connections idle in transaction (they hold locks and block vacuum), and the "
                       "longest running query in seconds. Note: the query time shares the axis with the counts.",
        "Transactions/s": "Commits and rollbacks per second over all databases and instances. "
                          "A growing rollback share points to application errors.",
        "Database Size over Time": "Size per database on the primary.",
        "Block I/O": "Blocks read from disk vs. found in shared_buffers per second. "
                     "Sustained high disk reads = working set larger than memory.",
        "WAL Activity": "WAL written by the primary, averaged over the last 5 minutes (bytes/s). "
                        "Drives archive volume and replication traffic.",
        "Replication Lag": "Per replica: time since the last replayed transaction (Replay) and the last received WAL "
                           "(Receive). Both also grow while the primary has no writes, so check together with WAL Activity.",
        "Temp Bytes Written/s": "Sorts and hashes spilling to disk. Persistent values = work_mem too small or expensive queries.",
        "Deadlocks": "Deadlocks per 5 minutes. Any non-zero value is worth a look in the PostgreSQL log.",
        "Since Last Backup (full/diff/incr)": "Age of the newest successful pgBackRest backup of any type. "
                                             "Orange from 1 day, red from 2 days. Details: Crunchy Backups dashboard.",
        "WAL Archiving": "Archived and failed WAL segments as absolute counters since the last statistics reset "
                         "(not a rate). A rising 'Failed' line means archive_command is failing.",
        "Since Last WAL Archive": "Time since the primary archived its last WAL segment. Orange from 5 min, red from 10 min. "
                                  "Also rises on an idle database without any error.",
        "Tuple Operations/s": "Rows inserted, updated, deleted and fetched per second over all databases and instances.",
    },
    "columns": {},
}

BACKUPS = {
    "file": "dashboard-crunchy-backups.json",
    "vars": {
        "datasource": "Prometheus- (oder Thanos-Querier-) Datenquelle fuer alle Panels.",
        "cluster": "PostgresCluster (Label 'pg_cluster' des Crunchy-pgMonitor-Exporters).",
        "namespace": "Namespace des Clusters. Versteckt; folgt dem gewaehlten Cluster.",
        "pgname": "Clustername fuer die Suche nach Backup-CronJobs (<name>-repo...). Versteckt; folgt dem Cluster.",
    },
    "links": {
        "Cluster Overview": "Health, Performance und Replikation desselben Clusters.",
    },
    "panels": {},
    "columns": {
        "Backup-CronJobs im Namespace": {
            "CronJob": "Name des CronJobs (pgBackRest-Repo-Jobs heissen <cluster>-repo<n>-<typ>).",
            "Seit letztem Lauf": "Zeit seit dem letzten geplanten Start, egal ob erfolgreich.",
            "Seit letztem Erfolg": "Zeit seit dem letzten erfolgreichen Lauf. Leer = nie erfolgreich.",
            "Naechster Lauf in": "Zeit bis zum naechsten geplanten Start. Negativ = ueberfaellig.",
            "Suspendiert": "1 = CronJob ist angehalten (spec.suspend) und laeuft nicht.",
        },
    },
}


def panels(d):
    for p in d["panels"]:
        yield p
        yield from p.get("panels", [])


def apply(d, cfg):
    if cfg.get("dashboard") and "CloudNativePG" in d.get("description", ""):
        d["description"] = cfg["dashboard"]  # copy-paste leftover from the CNPG dashboard
    for p in panels(d):
        if p["type"] == "row":
            continue
        if not p.get("description") and p["title"] in cfg["panels"]:
            p["description"] = cfg["panels"][p["title"]]
        cols = cfg["columns"].get(p["title"])
        if cols:
            desc = p.get("description", "").split("\n\n- **")[0].strip()  # idempotent: rebuild the list
            p["description"] = desc + "\n\n" + "\n".join(f"- **{c}**: {t}" for c, t in cols.items())
    for v in d["templating"]["list"]:
        if not v.get("description") and v["name"] in cfg["vars"]:
            v["description"] = cfg["vars"][v["name"]]
    for lk in d.get("links", []):
        if not lk.get("tooltip") and lk["title"] in cfg["links"]:
            lk["tooltip"] = cfg["links"][lk["title"]]


def missing(d):
    out = [f"panel '{p['title']}'" for p in panels(d) if p["type"] != "row" and not p.get("description", "").strip()]
    for p in panels(d):
        if p["type"] != "table":
            continue
        org = next((t["options"] for t in p.get("transformations", []) if t["id"] == "organize"), {})
        for c in org.get("renameByName", {}).values():
            if f"**{c}**" not in p.get("description", ""):
                out.append(f"table '{p['title']}' column '{c}'")
    out += [f"variable '{v['name']}'" for v in d["templating"]["list"] if not v.get("description", "").strip()]
    out += [f"link '{lk['title']}'" for lk in d.get("links", []) if not lk.get("tooltip", "").strip()]
    return out


def main(root, check):
    bad = 0
    for cfg in (OVERVIEW, BACKUPS):
        path = os.path.join(root, "dashboards", cfg["file"])
        raw = open(path, encoding="utf-8").read()
        d = json.loads(raw)
        if not check:
            apply(d, cfg)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                # Same style as the files (2-space indent, raw UTF-8): the diff shows only tooltip changes.
                json.dump(d, f, indent=2, ensure_ascii=False)
                f.write("\n")
        gaps = missing(d)
        bad += len(gaps)
        print(f"{cfg['file']}: " + ("all tooltips present" if not gaps else "MISSING " + ", ".join(gaps)))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main(sys.argv[1], "--check" in sys.argv[2:])
