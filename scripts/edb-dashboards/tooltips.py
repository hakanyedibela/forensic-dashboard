"""Tooltip texts for the EDB dashboards.

Applied by gen_dashboards.py after the panels are built:
  PANELS   -> panel description (the (i) next to the title), only where the
              panel does not already set one inline
  COLUMNS  -> column explanations, appended to the table's panel tooltip as a
              markdown list (Grafana 13 tables do not show field descriptions
              in the column header, so a per-field description would be invisible)
  VARS     -> variable label tooltips
  LINKS    -> dashboard link tooltips
Overview and Alert Center are English, Backups is German (matches the panels).
"""

# --------------------------------------------------------------------------- shared
VARS_EN = {
    "datasource": "Prometheus (or Thanos Querier) datasource all panels query.",
    "namespace": "Kubernetes namespace of the EDB cluster.",
    "cluster": "EDB Cluster resource (label 'cluster' of cnp_collector_up).",
    "job": "Scrape job of the PodMonitor (<namespace>/<podmonitor>). Hidden; follows the selected cluster.",
}
VARS_DE = {
    "datasource": "Prometheus- (oder Thanos-Querier-) Datenquelle fuer alle Panels.",
    "namespace": "Kubernetes-Namespace des EDB-Clusters.",
    "cluster": "EDB-Cluster-Ressource (Label 'cluster' von cnp_collector_up).",
    "job": "Scrape-Job des PodMonitors (<namespace>/<podmonitor>). Versteckt; folgt dem gewaehlten Cluster.",
}
VARS_ALERTS = {
    "datasource": "Prometheus (or Thanos Querier) datasource holding the ALERTS series.",
    "scope": "EDB Postgres = only rules labelled component=edb-postgres. "
             "All alerts = every Prometheus alert (Kubernetes, nodes, ...).",
    "namespace": "Namespaces that had alerts in the selected time range. All = every namespace.",
    "severity": "Filter by alert severity. critical = act now, warning = investigate soon, info = no action.",
}

LINKS = {
    "Backups": "Backup and WAL-archiving details for the same namespace, cluster and time range.",
    "Alert Center": "All active and past alerts with descriptions and history.",
    "Cluster Overview": "Health, performance and replication of the same cluster.",
}

ALERT_COLS_EN = {
    "severity": "critical = act now, warning = investigate soon, info = no action needed.",
    "alertname": "Name of the Prometheus rule (manifests/edb-postgres-alerts.yaml).",
    "pod": "Instance the alert refers to. Empty for cluster-wide alerts (backups, primary election).",
    "datname": "Database the alert refers to, if any.",
}
ALERT_COLS_DE = {
    "severity": "critical = sofort handeln, warning = zeitnah pruefen, info = keine Aktion.",
    "alertname": "Name der Prometheus-Regel (manifests/edb-postgres-alerts.yaml).",
    "pod": "Betroffene Instanz. Leer bei clusterweiten Alarmen (Backup, Primary-Wahl).",
    "datname": "Betroffene Datenbank, falls zutreffend.",
}

# --------------------------------------------------------------------------- overview
OVERVIEW_PANELS = {
    "Postgres version": "PostgreSQL version from cnp_collector_postgres_version (label 'full'). "
                        "Two versions at once only during a rolling minor upgrade.",
    "Flags": "Fencing: instance deliberately stopped (k8s.enterprisedb.io/fencedInstances). "
             "Switchover req.: operator waits for a manual switchover (primaryUpdateStrategy: supervised). "
             "Exporter error: the last monitoring query failed, other panels may be stale. "
             "Replica mode: the whole cluster replicates from another cluster (expected for DR clusters).",
    "Instances": "One row per pod.",
    "Connections used (% of max_connections)": "Client backends per instance divided by max_connections. "
                                               "Warning at 80 %, critical at 95 % (CNPConnectionsNearLimit). "
                                               "At 100 % new connections are refused.",
    "Transactions / s": "Commits and rollbacks per second over all databases and instances. "
                        "A growing rollback share points to application errors or serialization conflicts.",
    "Tuples / s": "Row operations per second (pg_stat_database). fetched/returned = reads, "
                  "inserted/updated/deleted = writes. returned much larger than fetched hints at sequential scans.",
    "Block reads / s": "Blocks read from disk vs. served from shared_buffers per second. "
                       "Sustained high disk reads = working set larger than memory.",
    "Replication lag (bytes, from primary)": "WAL not yet replayed per standby, measured on the primary "
                                             "(pg_stat_replication). Byte view of the replay lag.",
    "Deadlocks & conflicts / h": "Deadlocks per hour (alert CNPDeadlocks above 10/h) and recovery conflicts: "
                                 "queries on replicas cancelled because WAL replay needed the rows.",
    "Last backup failed": "YES = newest failed backup is younger than the newest successful one "
                          "(alert CNPLastBackupFailed). Clears with the next successful backup. "
                          "n/a = no backup metrics.",
    "WAL archiving failing": "YES = last archive_command failure on the primary is newer than the last success "
                             "(alert CNPWALArchivingFailing). Point-in-time recovery is broken while YES.",
}
OVERVIEW_COLUMNS = {
    "Instances": {
        "pod": "Pod name of the instance (<cluster>-<n>).",
        "Up": "up = PostgreSQL answers the instance manager (cnp_collector_up).",
        "Role": "primary = accepts writes; replica = streaming standby (pg_is_in_recovery()).",
        "Replay lag": "Seconds the replica is behind the primary. 0 on the primary. Orange from 60 s, red from 300 s.",
        "Backends": "Client connections (pg_stat_activity) on this instance.",
        "Conn. used": "Backends / max_connections. Orange from 80 %, red from 95 %.",
        "pg_wal": "Size of WAL segments on disk (pg_wal directory).",
        "Uptime": "Time since the postmaster on this instance started.",
    },
    "Active alerts": ALERT_COLS_EN,
    "Extensions with updates available": {
        "datname": "Database the extension is installed in.",
        "extname": "Extension name.",
        "installed_version": "Version currently installed in the database.",
        "default_version": "Version shipped with the running PostgreSQL image. Run ALTER EXTENSION ... UPDATE.",
    },
}

# --------------------------------------------------------------------------- backups (German)
BACKUP_PANELS = {
    "Letztes Backup": "Zeitpunkt des juengsten erfolgreichen Backups (Browser-Zeitzone).",
    "Letzter Fehlversuch": "Zeitpunkt des letzten fehlgeschlagenen Backups. 'nie' = bisher kein Fehler. "
                           "Aelter als 'Letztes Backup' = Fehler ist bereits ueberholt.",
    "WAL-Archivfehler (24 h)": "Fehlgeschlagene archive_command-Aufrufe in 24 h. Einzelne Fehler mit erfolgreichem "
                               "Retry sind unkritisch; nur zusammen mit 'Archivierung fehlerhaft? = JA' ein Problem.",
    "Archivierte Segmente (24 h)": "In 24 h ins Object Store geschriebene WAL-Segmente (je 16 MiB). "
                                   "Mass fuer WAL-Aufkommen und Speicherbedarf der Backups.",
    "Restore-Fenster": "Abstand zwischen fruehestem Wiederherstellungspunkt und jetzt. Faellt die Kurve, hat die "
                       "retentionPolicy alte Backups geloescht; waechst sie unbegrenzt, greift keine Retention.",
    "WAL-Archivierung / h": "Archivierte (gruen) und fehlgeschlagene (rot) WAL-Segmente pro Stunde.",
    "pg_wal Belegung": "Groesse von pg_wal gegen die Groesse des WAL-Volumes (gestrichelt). "
                       "Erreicht die Linie das Volume, stoppt PostgreSQL.",
}
BACKUP_COLUMNS = {"Aktive Alarme": ALERT_COLS_DE}

# --------------------------------------------------------------------------- alert center
# Counters, table and history read Prometheus' ALERTS series, which only exists for rules
# Prometheus evaluates. Rules imported into Grafana show up in the inbox only.
_ALERTS_SRC = (" Source: Prometheus ALERTS series (rules deployed as PrometheusRule). "
               "Rules imported into Grafana appear only in the Alert inbox.")
ALERT_PANELS = {
    "Critical": "Firing alerts with severity=critical in the current scope. Needs action now.",
    "Warning": "Firing alerts with severity=warning. Investigate soon.",
    "Info": "Informational alerts (e.g. PostgreSQL restarted). No action required.",
    "Affected namespaces": "Namespaces with at least one firing alert.",
    "Firing alerts over time": "One row per alert instance; red = firing, empty = not firing. Hover for start and end.",
    "Firing alerts by severity": "Number of firing alerts per severity over time (stacked). Peaks mark incident windows.",
}
ALERT_PANELS = {k: v + _ALERTS_SRC for k, v in ALERT_PANELS.items()}
# Alert Center tables show a fixed column set without datname.
_AC_COLS = {k: v for k, v in ALERT_COLS_EN.items() if k != "datname"}
ALERT_COLUMNS = {
    "Active alerts": {
        **_AC_COLS,
        "alertstate": "FIRING = condition held for the rule's 'for' duration. "
                      "pending = condition true, 'for' not yet reached.",
        "namespace": "Kubernetes namespace of the affected workload.",
        "active since": "When the condition first became true (ALERTS_FOR_STATE), including the pending phase.",
        "duration": "Time since 'active since'.",
        "what it means / what to do": "Description of the rule; <label> placeholders refer to the columns on the left.",
    },
    "Alert log": {
        **_AC_COLS,
        "status": "FIRING = still firing at the end of the time range; resolved = not firing any more.",
        "namespace": "Kubernetes namespace of the affected workload.",
        "first seen": "First minute the alert was firing within the selected time range.",
        "last seen": "Last minute the alert was firing within the selected time range.",
        "minutes firing": "Minutes in firing state within the selected time range (1-minute resolution).",
    },
}


def apply(dash, panels, columns, variables):
    for p in dash["panels"]:
        if p["type"] == "row":
            continue
        if not p.get("description") and p["title"] in panels:
            p["description"] = panels[p["title"]]
        cols = columns.get(p["title"])
        if cols:
            head = p.get("description", "").strip()
            p["description"] = (head + "\n\n" if head else "") + "\n".join(
                f"- **{col}**: {text}" for col, text in cols.items())
    for v in dash["templating"]["list"]:
        v.setdefault("description", variables.get(v["name"], ""))
    for link in dash["links"]:
        link["tooltip"] = link.get("tooltip") or LINKS.get(link["title"], "")
