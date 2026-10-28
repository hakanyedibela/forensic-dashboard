"""Generate EDB Postgres for Kubernetes Grafana dashboards (overview + backups).

Metric names follow the EDB operator's default-monitoring.yaml and the
cnp_collector_* set documented at
https://www.enterprisedb.com/docs/postgres_for_kubernetes/latest/monitoring/
"""
import json
import sys

import tooltips

DS = {"type": "prometheus", "uid": "${datasource}"}
# Every cnp_* series scraped via PodMonitor carries namespace/pod/job.
S = 'namespace="$namespace", job="$job"'
PRIMARY = f'on (namespace, pod) cnp_pg_replication_in_recovery{{{S}}} == 0'
ALERTS = f'ALERTS{{component="edb-postgres", namespace="$namespace", job="$job"'
BK_AVAIL = f'{{__name__=~".+_last_available_backup_timestamp", {S}}}'
BK_FAIL = f'{{__name__=~".+_last_failed_backup_timestamp", {S}}}'
BK_FIRST = f'{{__name__=~".+_first_recoverability_point", {S}}}'


class Layout:
    def __init__(self):
        self.panels, self.y, self.x, self.row_h, self.next_id = [], 0, 0, 0, 1

    def _id(self):
        self.next_id += 1
        return self.next_id - 1

    def row(self, title, collapsed=False):
        self._newline()
        self.panels.append({"type": "row", "title": title, "id": self._id(),
                            "collapsed": collapsed, "panels": [],
                            "gridPos": {"h": 1, "w": 24, "x": 0, "y": self.y}})
        self.y += 1

    def _newline(self):
        if self.x:
            self.y += self.row_h
            self.x, self.row_h = 0, 0

    def add(self, panel, w, h):
        if self.x + w > 24:
            self._newline()
        panel["id"] = self._id()
        panel["gridPos"] = {"h": h, "w": w, "x": self.x, "y": self.y}
        panel.setdefault("datasource", DS)
        self.panels.append(panel)
        self.x += w
        self.row_h = max(self.row_h, h)

    def place(self, panel, x, y, w, h):
        """Absolute placement; does not move the cursor."""
        panel["id"] = self._id()
        panel["gridPos"] = {"h": h, "w": w, "x": x, "y": y}
        panel.setdefault("datasource", DS)
        self.panels.append(panel)


def tgt(expr, legend="", ref="A", instant=False, fmt=None):
    t = {"datasource": DS, "refId": ref, "expr": expr, "legendFormat": legend}
    if instant:
        t.update(instant=True, range=False)
    if fmt:
        t["format"] = fmt
    return t


def steps(*pairs):
    return {"mode": "absolute",
            "steps": [{"color": c, "value": v} for v, c in pairs]}


def stat(title, targets, unit="short", th=None, desc="", no_value=None,
         mappings=None, color_mode="background", text_mode="auto",
         graph="none", overrides=None, decimals=None):
    d = {"unit": unit, "color": {"mode": "thresholds"},
         "thresholds": th or steps((None, "green")), "mappings": mappings or []}
    if no_value is not None:
        d["noValue"] = no_value
    if decimals is not None:
        d["decimals"] = decimals
    return {"type": "stat", "title": title, "description": desc,
            "targets": targets,
            "fieldConfig": {"defaults": d, "overrides": overrides or []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": color_mode, "graphMode": graph, "textMode": text_mode,
                        "justifyMode": "auto", "orientation": "auto", "wideLayout": True}}


def ts(title, targets, unit="short", desc="", th=None, th_style="off",
       stack=False, fill=10, overrides=None, min0=True, bars=False):
    custom = {"drawStyle": "bars" if bars else "line", "lineWidth": 1,
              "fillOpacity": fill, "showPoints": "never", "spanNulls": False,
              "stacking": {"mode": "normal" if stack else "none", "group": "A"},
              "thresholdsStyle": {"mode": th_style}}
    d = {"unit": unit, "custom": custom, "color": {"mode": "palette-classic"},
         "thresholds": th or steps((None, "green"))}
    if min0:
        d["min"] = 0
    return {"type": "timeseries", "title": title, "description": desc,
            "targets": targets,
            "fieldConfig": {"defaults": d, "overrides": overrides or []},
            "options": {"legend": {"displayMode": "table", "placement": "bottom",
                                   "showLegend": True, "calcs": ["lastNotNull", "max"]},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}


def table(title, targets, transformations, overrides=None, desc="", unit="short",
          no_value=None, sort_by=None):
    d = {"unit": unit, "custom": {"align": "auto", "cellOptions": {"type": "auto"}}}
    if no_value is not None:
        d["noValue"] = no_value
    return {"type": "table", "title": title, "description": desc, "targets": targets,
            "transformations": transformations,
            "fieldConfig": {"defaults": d, "overrides": overrides or []},
            "options": {"showHeader": True, "cellHeight": "sm",
                        "footer": {"show": False, "reducer": ["sum"], "fields": ""},
                        "sortBy": sort_by or []}}


def by_name(name, *props):
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": k, "value": v} for k, v in props]}


def vmap(mapping):
    return [{"type": "value", "options": {
        str(k): {"text": t, "color": c, "index": i}
        for i, (k, (t, c)) in enumerate(mapping.items())}}]


# `or on()` only adds this when the left side is empty (no alert fired in range).
NO_ALERTS = ' or on () label_replace(vector(0), "alertname", "no alerts", "", "")'
TIMELINE_MAP = vmap({0: ("no alerts", "green"), 1: ("firing", "red")})
YES_NO_BAD = vmap({0: ("nein", "green"), 1: ("JA", "red")})
YES_NO_BAD_EN = vmap({0: ("no", "green"), 1: ("YES", "red")})
SEVERITY_COLORS = [by_name("severity", ("mappings", vmap({
    "critical": ("critical", "red"), "warning": ("warning", "orange"),
    "info": ("info", "blue")})), ("custom.cellOptions", {"type": "color-background"}))]

ALERT_TABLE_HIDE = {k: True for k in [
    "Time", "Value", "__name__", "component", "job", "namespace", "instance",
    "endpoint", "container", "service", "prometheus", "alertstate", "cluster",
    "stats_reset", "value"]}


def alert_section(L, lang, name_re=None):
    """Firing-alert stats, live table and history timeline, scoped to $job."""
    sel = ALERTS + (f', alertname=~"{name_re}"' if name_re else "")
    t = {"en": ("Critical firing", "Warnings firing", "Pending", "Active alerts",
                "Alert history (firing)", "No active alerts",
                "From PrometheusRule edb-postgres-alerts (ALERTS series). Empty = rules not deployed or nothing firing."),
         "de": ("Kritisch (aktiv)", "Warnungen (aktiv)", "Pending", "Aktive Alarme",
                "Alarm-Verlauf (firing)", "Keine aktiven Alarme",
                "Aus PrometheusRule edb-postgres-alerts (ALERTS-Serie). Leer = Regeln nicht deployed oder nichts aktiv.")}[lang]
    L.add(stat(t[0], [tgt(f'count({sel}, alertstate="firing", severity="critical"}})', instant=True)],
               th=steps((None, "green"), (1, "red")), no_value="0", desc=t[6]), 4, 4)
    L.add(stat(t[1], [tgt(f'count({sel}, alertstate="firing", severity="warning"}})', instant=True)],
               th=steps((None, "green"), (1, "orange")), no_value="0", desc=t[6]), 4, 4)
    L.add(stat(t[2], [tgt(f'count({sel}, alertstate="pending"}})', instant=True)],
               th=steps((None, "green"), (1, "yellow")), no_value="0",
               desc="Condition true, 'for' duration not yet elapsed."), 4, 4)
    L.add(table(t[3], [tgt(f'{sel}}}', instant=True, fmt="table")],
                [{"id": "organize", "options": {
                    "excludeByName": ALERT_TABLE_HIDE,
                    "indexByName": {"severity": 0, "alertname": 1, "pod": 2, "datname": 3}}}],
                # no noValue here: Grafana applies it to every empty cell (e.g. alerts without pod)
                overrides=SEVERITY_COLORS, desc=t[6],
                sort_by=[{"displayName": "severity", "desc": False}]), 12, 8)
    p = {"type": "state-timeline", "title": t[4], "description": t[6],
         "targets": [tgt(f'max by (alertname, severity, pod) ({sel}, alertstate="firing"}})' + NO_ALERTS,
                         "{{alertname}} {{pod}}")],
         "fieldConfig": {"defaults": {
             "color": {"mode": "thresholds"},
             "thresholds": steps((None, "green"), (1, "red")),
             "mappings": TIMELINE_MAP,
             "custom": {"fillOpacity": 80, "lineWidth": 0}}, "overrides": []},
         "options": {"showValue": "never", "mergeValues": True, "rowHeight": 0.8,
                     "alignValue": "left", "legend": {"showLegend": False},
                     "tooltip": {"mode": "single"}}}
    # Under the three stats, left of the 8-high table.
    L.place(p, 0, L.y + 4, 12, 4)


def variables(extra=()):
    def q(name, label, query, hide=0):
        return {"name": name, "label": label, "type": "query", "datasource": DS,
                "query": {"query": query, "refId": "PrometheusVariableQueryEditor-VariableQuery"},
                "definition": query, "refresh": 2, "sort": 1, "hide": hide,
                "includeAll": False, "multi": False, "current": {}, "options": []}
    return {"list": [
        {"name": "datasource", "label": "Datasource", "type": "datasource",
         "query": "prometheus", "current": {}, "hide": 0, "options": [], "refresh": 1},
        q("namespace", "Namespace", "label_values(cnp_collector_up, namespace)"),
        q("cluster", "Cluster", 'label_values(cnp_collector_up{namespace="$namespace"}, cluster)'),
        q("job", "Job", 'label_values(cnp_collector_up{namespace="$namespace", cluster="$cluster"}, job)', hide=2),
        *extra]}


def dashboard(uid, title, tags, panels, links, time_from, refresh, var_list):
    return {"uid": uid, "title": title, "tags": tags, "editable": True,
            "schemaVersion": 39, "version": 1, "graphTooltip": 1,
            "time": {"from": time_from, "to": "now"}, "refresh": refresh,
            "timezone": "browser", "fiscalYearStartMonth": 0, "liveNow": False,
            "annotations": {"list": [{
                "builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
                "name": "Annotations & Alerts", "type": "dashboard"}, {
                "name": "PostgreSQL restart", "datasource": DS, "enable": True,
                "iconColor": "purple", "step": "60s", "titleFormat": "Postmaster restart {{pod}}",
                "expr": f'changes(cnp_pg_postmaster_start_time{{{S}}}[2m]) > 0'}]},
            "links": links, "templating": var_list, "panels": panels}


def link(title, uid, icon="dashboard"):
    return {"title": title, "type": "link", "icon": icon, "targetBlank": False,
            "url": f"/d/{uid}?${{__url_time_range}}&var-datasource=${{datasource}}"
                   "&var-namespace=${namespace}&var-cluster=${cluster}",
            "keepTime": True, "includeVars": False, "tooltip": ""}


# --------------------------------------------------------------------------- overview
def overview():
    L = Layout()
    L.row("Alerts")
    alert_section(L, "en")

    L.row("Cluster health")
    L.add(stat("Instances ready", [tgt(f'sum(cnp_collector_up{{{S}}})', "ready", instant=True),
                                   tgt(f'count(cnp_collector_up{{{S}}})', "total", "B", instant=True)],
               color_mode="value", text_mode="value_and_name",
               desc="cnp_collector_up: 1 if PostgreSQL answers the exporter."), 4, 4)
    L.add(stat("Primary", [tgt(f'cnp_pg_replication_in_recovery{{{S}}} == 0', "{{pod}}", instant=True)],
               text_mode="name", no_value="NO PRIMARY", th=steps((None, "green")),
               desc="Pod with pg_is_in_recovery() = false. Empty = failover in progress or replica cluster."), 4, 4)
    L.add(stat("Postgres version", [tgt(f'max by (full) (cnp_collector_postgres_version{{{S}}})', "{{full}}", instant=True)],
               text_mode="name", color_mode="none"), 3, 4)
    L.add(stat("Nodes used", [tgt(f'max(cnp_collector_nodes_used{{{S}}})', instant=True)],
               th=steps((None, "red"), (2, "green")),
               desc="Distinct k8s nodes hosting the instances. 1 = no HA (all pods on one node). -1 = unknown."), 3, 4)
    L.add(stat("Sync replicas", [
        tgt(f'max(cnp_collector_sync_replicas{{{S}, value="observed"}})', "observed", instant=True),
        tgt(f'max(cnp_collector_sync_replicas{{{S}, value="expected"}})', "expected", "B", instant=True)],
        color_mode="value", text_mode="value_and_name",
        desc="observed < expected = synchronous replication degraded (writes may block)."), 4, 4)
    L.add(stat("Since newest restart", [tgt(f'time() - max(cnp_pg_postmaster_start_time{{{S}}})', instant=True)],
               unit="s", th=steps((None, "orange"), (3600, "green")), color_mode="value",
               desc="Uptime of the most recently (re)started instance."), 3, 4)
    L.add(stat("Flags", [
        tgt(f'max(cnp_collector_fencing_on{{{S}}})', "Fencing", instant=True),
        tgt(f'max(cnp_collector_manual_switchover_required{{{S}}})', "Switchover req.", "B", instant=True),
        tgt(f'max(cnp_collector_last_collection_error{{{S}}})', "Exporter error", "C", instant=True),
        tgt(f'max(cnp_collector_replica_mode{{{S}}})', "Replica mode", "D", instant=True)],
        th=steps((None, "green"), (1, "red")), text_mode="value_and_name",
        mappings=vmap({0: ("no", "green"), 1: ("YES", "red")}),
        overrides=[by_name("Replica mode", ("mappings", vmap({0: ("no", "green"), 1: ("yes", "blue")})))]), 3, 4)

    inst = table("Instances", [
        tgt(f'max by (pod) (cnp_collector_up{{{S}}})', ref="A", instant=True, fmt="table"),
        tgt(f'max by (pod) (1 - cnp_pg_replication_in_recovery{{{S}}})', ref="B", instant=True, fmt="table"),
        tgt(f'max by (pod) (cnp_pg_replication_lag{{{S}}})', ref="C", instant=True, fmt="table"),
        tgt(f'sum by (pod) (cnp_backends_total{{{S}}})', ref="D", instant=True, fmt="table"),
        tgt(f'sum by (pod) (cnp_backends_total{{{S}}}) / on (pod) max by (pod) (cnp_pg_settings_setting{{{S}, name="max_connections"}})',
            ref="E", instant=True, fmt="table"),
        tgt(f'max by (pod) (cnp_collector_pg_wal{{{S}, value="size"}})', ref="F", instant=True, fmt="table"),
        tgt(f'time() - max by (pod) (cnp_pg_postmaster_start_time{{{S}}})', ref="G", instant=True, fmt="table")],
        [{"id": "merge", "options": {}},
         {"id": "organize", "options": {"excludeByName": {"Time": True}, "renameByName": {
             "Value #A": "Up", "Value #B": "Role", "Value #C": "Replay lag",
             "Value #D": "Backends", "Value #E": "Conn. used", "Value #F": "pg_wal",
             "Value #G": "Uptime"}}}],
        overrides=[
            by_name("Up", ("mappings", vmap({0: ("DOWN", "red"), 1: ("up", "green")})),
                    ("custom.cellOptions", {"type": "color-text"})),
            by_name("Role", ("mappings", vmap({0: ("replica", "blue"), 1: ("primary", "green")})),
                    ("custom.cellOptions", {"type": "color-text"})),
            by_name("Replay lag", ("unit", "s"), ("color", {"mode": "thresholds"}),
                    ("thresholds", steps((None, "green"), (60, "orange"), (300, "red"))),
                    ("custom.cellOptions", {"type": "color-text"})),
            by_name("Conn. used", ("unit", "percentunit"), ("color", {"mode": "thresholds"}),
                    ("thresholds", steps((None, "green"), (0.8, "orange"), (0.95, "red"))),
                    ("custom.cellOptions", {"type": "gauge", "mode": "basic"}), ("max", 1), ("min", 0)),
            by_name("pg_wal", ("unit", "bytes")),
            by_name("Uptime", ("unit", "s"))],
        sort_by=[{"displayName": "Role", "desc": True}])
    L.add(inst, 24, 6)

    L.row("Connections & transactions")
    L.add(ts("Backends by state", [tgt(f'sum by (state) (cnp_backends_total{{{S}}})', "{{state}}")],
             stack=True, desc="pg_stat_activity client backends, all instances."), 8, 8)
    L.add(ts("Connections used (% of max_connections)", [tgt(
        f'sum by (pod) (cnp_backends_total{{{S}}}) / on (pod) max by (pod) (cnp_pg_settings_setting{{{S}, name="max_connections"}})',
        "{{pod}}")], unit="percentunit", th=steps((None, "green"), (0.8, "orange"), (0.95, "red")),
        th_style="line+area", fill=0), 8, 8)
    L.add(ts("Longest open transaction", [tgt(
        f'max by (pod) (cnp_backends_max_tx_duration_seconds{{{S}}})', "{{pod}}")], unit="s",
        th=steps((None, "green"), (300, "red")), th_style="line", fill=0,
        desc="Alert CNPLongRunningTransaction at 300 s. Long transactions block vacuum and bloat tables."), 8, 8)
    L.add(ts("Transactions / s", [
        tgt(f'sum(rate(cnp_pg_stat_database_xact_commit{{{S}}}[$__rate_interval]))', "commit"),
        tgt(f'sum(rate(cnp_pg_stat_database_xact_rollback{{{S}}}[$__rate_interval]))', "rollback", "B")],
        unit="ops", overrides=[by_name("rollback", ("color", {"mode": "fixed", "fixedColor": "red"}))]), 8, 8)
    L.add(ts("Tuples / s", [
        tgt(f'sum(rate(cnp_pg_stat_database_tup_{op}{{{S}}}[$__rate_interval]))', op, chr(65 + i))
        for i, op in enumerate(["fetched", "returned", "inserted", "updated", "deleted"])], unit="ops"), 8, 8)
    L.add(ts("Lock-blocked backends", [tgt(f'max by (pod) (cnp_backends_waiting_total{{{S}}})', "{{pod}}")],
             th=steps((None, "green"), (10, "orange")), th_style="line", fill=0,
             desc="Backends waiting on a lock held by another backend (pg_locks). Alert CNPBackendsBlockedOnLocks at >10 for 5 m."), 8, 8)

    L.row("Storage & I/O")
    L.add(ts("Database size", [tgt(f'max by (datname) (cnp_pg_database_size_bytes{{{S}}})', "{{datname}}")],
             unit="bytes", stack=True, desc="max across instances (replicas hold the same data, summing would multiply it)."), 8, 8)
    L.add(ts("pg_wal usage", [
        tgt(f'max by (pod) (cnp_collector_pg_wal{{{S}, value="size"}}) / max by (pod) (cnp_collector_pg_wal{{{S}, value="volume_size"}})',
            "{{pod}}")], unit="percentunit", th=steps((None, "green"), (0.8, "orange"), (0.9, "red")),
        th_style="line+area", fill=0,
        desc="WAL segments on disk vs. size of the volume holding pg_wal. Full pg_wal = PostgreSQL stops. Usually caused by failing archiving or a stale replication slot."), 8, 8)
    L.add(ts("Cache hit ratio", [tgt(
        f'sum by (pod) (rate(cnp_pg_stat_database_blks_hit{{{S}}}[$__rate_interval])) / '
        f'(sum by (pod) (rate(cnp_pg_stat_database_blks_hit{{{S}}}[$__rate_interval])) + sum by (pod) (rate(cnp_pg_stat_database_blks_read{{{S}}}[$__rate_interval])))',
        "{{pod}}")], unit="percentunit", min0=False, fill=0,
        th=steps((None, "red"), (0.9, "orange"), (0.99, "green")), th_style="line",
        desc="Current (rate-based) shared_buffers hit ratio. OLTP target > 99 %."), 8, 8)
    L.add(ts("Block reads / s", [
        tgt(f'sum(rate(cnp_pg_stat_database_blks_read{{{S}}}[$__rate_interval]))', "disk read"),
        tgt(f'sum(rate(cnp_pg_stat_database_blks_hit{{{S}}}[$__rate_interval]))', "buffer hit", "B")],
        unit="ops"), 8, 8)
    L.add(ts("Temp files written", [tgt(
        f'sum by (datname) (rate(cnp_pg_stat_database_temp_bytes{{{S}}}[$__rate_interval]))', "{{datname}}")],
        unit="Bps", desc="Sorts/hashes spilling to disk. Persistent values = work_mem too small or bad queries."), 8, 8)
    L.add(ts("WAL generated", [tgt(
        f'sum by (pod) (rate(cnp_collector_wal_bytes{{{S}}}[$__rate_interval]))', "{{pod}}")],
        unit="Bps", desc="pg_stat_wal.wal_bytes (PG 14+). Drives archive volume and replication bandwidth."), 8, 8)

    L.row("Replication")
    L.add(ts("Replica replay lag", [tgt(f'max by (pod) (cnp_pg_replication_lag{{{S}}})', "{{pod}}")],
             unit="s", th=steps((None, "green"), (300, "orange"), (900, "red")), th_style="line", fill=0,
             desc="now() - pg_last_xact_replay_timestamp() on replicas; 0 when caught up."), 8, 8)
    L.add(ts("Replication lag (bytes, from primary)", [tgt(
        f'max by (application_name) (cnp_pg_stat_replication_replay_diff_bytes{{{S}}})', "{{application_name}}")],
        unit="bytes", fill=0), 8, 8)
    L.add(ts("Replication slot WAL retention", [tgt(
        f'max by (slot_name) (cnp_pg_replication_slots_pg_wal_lsn_diff{{{S}}} and {PRIMARY})', "{{slot_name}}")],
        unit="bytes", fill=0,
        desc="WAL kept back by each slot on the primary. Growing line = consumer gone, pg_wal fills up."), 8, 8)

    L.row("Checkpoints & maintenance", collapsed=False)
    L.add(ts("Checkpoints / h", [
        tgt(f'sum(increase(cnp_pg_stat_checkpointer_checkpoints_timed{{{S}}}[1h])) or sum(increase(cnp_pg_stat_bgwriter_checkpoints_timed{{{S}}}[1h]))', "timed"),
        tgt(f'sum(increase(cnp_pg_stat_checkpointer_checkpoints_req{{{S}}}[1h])) or sum(increase(cnp_pg_stat_bgwriter_checkpoints_req{{{S}}}[1h]))', "requested", "B")],
        bars=True, fill=80, stack=True,
        desc="PG 17+ pg_stat_checkpointer, older pg_stat_bgwriter. Many 'requested' = max_wal_size too small."), 8, 8)
    L.add(ts("Transaction ID age", [tgt(f'max by (datname) (cnp_pg_database_xid_age{{{S}}})', "{{datname}}")],
             th=steps((None, "green"), (300000000, "orange"), (1000000000, "red")), th_style="line", fill=0,
             desc="Wraparound protection forces read-only at ~2.1 B. Alert warning 300 M / critical 1 B."), 8, 8)
    L.add(ts("Deadlocks & conflicts / h", [
        tgt(f'sum by (datname) (increase(cnp_pg_stat_database_deadlocks{{{S}}}[1h]))', "deadlocks {{datname}}"),
        tgt(f'sum by (datname) (increase(cnp_pg_stat_database_conflicts{{{S}}}[1h]))', "replica conflicts {{datname}}", "B")],
        bars=True, fill=80), 8, 8)
    L.add(table("Extensions with updates available", [tgt(
        f'max by (datname, extname, installed_version, default_version) (cnp_pg_extensions_update_available{{{S}}}) == 1',
        instant=True, fmt="table")],
        [{"id": "organize", "options": {"excludeByName": {"Time": True, "Value": True}}}],
        no_value="all extensions current", desc="Run ALTER EXTENSION ... UPDATE after operand upgrades."), 24, 5)

    L.row("Backup (summary)")
    L.add(stat("Since last backup", [tgt(f'time() - max({BK_AVAIL} > 0)', instant=True)], unit="s",
               th=steps((None, "green"), (93600, "orange"), (180000, "red")), no_value="no backup",
               desc="Newest *_last_available_backup_timestamp (in-core cnp_collector or Barman Cloud plugin). Details: backup dashboard."), 6, 4)
    L.add(stat("Last backup failed", [tgt(
        f'max({BK_FAIL}) > bool max({BK_AVAIL})', instant=True)], mappings=YES_NO_BAD_EN,
        th=steps((None, "green"), (1, "red")), no_value="n/a"), 6, 4)
    L.add(stat("WAL archiving failing", [tgt(
        f'max((cnp_pg_stat_archiver_last_failed_time{{{S}}} - cnp_pg_stat_archiver_last_archived_time{{{S}}}) > bool 1 and {PRIMARY})',
        instant=True)], mappings=YES_NO_BAD_EN, th=steps((None, "green"), (1, "red")), no_value="n/a"), 6, 4)
    L.add(stat("WAL waiting for archive", [tgt(
        f'max(cnp_collector_pg_wal_archive_status{{{S}, value="ready"}})', instant=True)],
        th=steps((None, "green"), (5, "orange"), (20, "red")),
        desc=".ready files in pg_wal/archive_status. Alert CNPWALArchiveBacklog > 20 for 15 m."), 6, 4)
    return L.panels


# --------------------------------------------------------------------------- backups (German, matches existing file)
def backups():
    L = Layout()
    L.row("Alarme (Backup & WAL-Archivierung)")
    alert_section(L, "de", name_re="CNP(Backup|LastBackup|NoBackup|WAL).*")

    L.row("Backup-Status")
    L.add(stat("Seit letztem Backup", [tgt(f'time() - max({BK_AVAIL} > 0)', instant=True)], unit="s",
               th=steps((None, "green"), (93600, "orange"), (180000, "red")), no_value="kein Backup",
               desc="Alter des juengsten verfuegbaren Backups (Object Store oder Volume Snapshot). Annahme taegliches ScheduledBackup: gelb ab 26 h, rot ab 50 h (= Alarme CNPBackupTooOld / CNPBackupMissing)."), 4, 4)
    L.add(stat("Letztes Backup fehlgeschlagen?", [tgt(f'max({BK_FAIL}) > bool max({BK_AVAIL})', instant=True)],
               mappings=YES_NO_BAD, th=steps((None, "green"), (1, "red")), no_value="n/a",
               desc="JA = letzter Fehlversuch ist juenger als das letzte erfolgreiche Backup."), 4, 4)
    L.add(stat("Restore-Fenster (PITR)", [tgt(f'time() - min({BK_FIRST} > 0)', instant=True)], unit="s",
               th=steps((None, "orange"), (86400, "green")), no_value="keins",
               desc="Abstand zum fruehesten Wiederherstellungspunkt = wie weit zurueck PITR moeglich ist. Sollte etwa der retentionPolicy entsprechen."), 4, 4)
    L.add(stat("Letztes Backup", [tgt(f'max({BK_AVAIL} > 0) * 1000', instant=True)],
               unit="dateTimeAsIso", color_mode="none", no_value="-"), 4, 4)
    L.add(stat("Letzter Fehlversuch", [tgt(f'max({BK_FAIL} > 0) * 1000', instant=True)],
               unit="dateTimeAsIso", color_mode="none", no_value="nie"), 4, 4)
    L.add(stat("Metrik-Quelle", [tgt(f'count by (__name__) ({BK_AVAIL})', "{{__name__}}", instant=True)],
               text_mode="name", color_mode="none", no_value="keine Backup-Metriken",
               desc="Gefundene *_last_available_backup_timestamp-Metriken: cnp_collector_* (in-core, deprecated) und/oder Barman-Cloud-Plugin. Keine = kein Backup konfiguriert oder Plugin-Sidecar nicht gescrapt."), 4, 4)

    L.row("WAL-Archivierung (Primary)")
    L.add(stat("Archivierung fehlerhaft?", [tgt(
        f'max((cnp_pg_stat_archiver_last_failed_time{{{S}}} - cnp_pg_stat_archiver_last_archived_time{{{S}}}) > bool 1 and {PRIMARY})',
        instant=True)], mappings=YES_NO_BAD, th=steps((None, "green"), (1, "red")), no_value="n/a",
        desc="JA = letzter archive_command-Fehler juenger als letzte erfolgreiche Archivierung (= Alarm CNPWALArchivingFailing)."), 4, 4)
    L.add(stat("Seit letzter WAL-Archivierung", [tgt(
        f'max(cnp_pg_stat_archiver_seconds_since_last_archival{{{S}}} and {PRIMARY})', instant=True)], unit="s",
        th=steps((None, "green"), (600, "orange"), (3600, "red")),
        desc="archive_timeout ist bei EDB/CNPG 5 min. Hohe Werte bei voellig idle DB sind normal."), 4, 4)
    L.add(stat("WAL wartet auf Archivierung", [tgt(
        f'max(cnp_collector_pg_wal_archive_status{{{S}, value="ready"}})', instant=True)],
        th=steps((None, "green"), (5, "orange"), (20, "red")),
        desc=".ready-Dateien in pg_wal/archive_status. Waechst die Zahl, kommt die Archivierung nicht hinterher oder schlaegt fehl."), 4, 4)
    L.add(stat("WAL-Archivfehler (24 h)", [tgt(
        f'sum(increase(cnp_pg_stat_archiver_failed_count{{{S}}}[24h]))', instant=True)],
        th=steps((None, "green"), (1, "orange"), (10, "red")), decimals=0), 4, 4)
    L.add(stat("pg_wal Belegung", [tgt(
        f'max(cnp_collector_pg_wal{{{S}, value="size"}} / ignoring (value) cnp_collector_pg_wal{{{S}, value="volume_size"}})',
        instant=True)], unit="percentunit", th=steps((None, "green"), (0.8, "orange"), (0.9, "red")),
        desc="Volles pg_wal-Volume stoppt PostgreSQL. Typische Ursache: Archivierung defekt."), 4, 4)
    L.add(stat("Archivierte Segmente (24 h)", [tgt(
        f'sum(increase(cnp_pg_stat_archiver_archived_count{{{S}}}[24h]))', instant=True)],
        color_mode="value", decimals=0), 4, 4)

    L.row("Verlauf")
    L.add(ts("Alter des letzten Backups", [
        tgt(f'time() - max({BK_AVAIL} > 0)', "letztes erfolgreiches"),
        tgt(f'time() - max({BK_FAIL} > 0)', "letzter Fehlversuch", "B")],
        unit="s", th=steps((None, "green"), (93600, "orange"), (180000, "red")), th_style="line", fill=0,
        overrides=[by_name("letzter Fehlversuch", ("color", {"mode": "fixed", "fixedColor": "red"}))],
        desc="Saegezahn = gesund. Linie steigt ohne Reset = Backups laufen nicht."), 12, 8)
    L.add(ts("Restore-Fenster", [tgt(f'time() - min({BK_FIRST} > 0)', "PITR-Fenster")], unit="s", fill=10), 12, 8)
    L.add(ts("WAL-Archivierung / h", [
        tgt(f'sum(increase(cnp_pg_stat_archiver_archived_count{{{S}}}[1h]))', "archiviert"),
        tgt(f'sum(increase(cnp_pg_stat_archiver_failed_count{{{S}}}[1h]))', "fehlgeschlagen", "B")],
        bars=True, fill=80,
        overrides=[by_name("fehlgeschlagen", ("color", {"mode": "fixed", "fixedColor": "red"}))]), 8, 8)
    L.add(ts("WAL archive_status (ready / done)", [
        tgt(f'max by (value) (cnp_collector_pg_wal_archive_status{{{S}}})', "{{value}}")],
        fill=0, overrides=[by_name("ready", ("color", {"mode": "fixed", "fixedColor": "red"}))],
        desc="ready = wartet auf Archivierung (Alarm ab 20), done = archiviert, wird beim naechsten Checkpoint aufgeraeumt."), 8, 8)
    L.add(ts("pg_wal Belegung", [
        tgt(f'max(cnp_collector_pg_wal{{{S}, value="size"}})', "pg_wal"),
        tgt(f'max(cnp_collector_pg_wal{{{S}, value="volume_size"}})', "Volume", "B")],
        unit="bytes", fill=0,
        overrides=[by_name("Volume", ("custom.lineStyle", {"fill": "dash", "dash": [10, 10]}),
                           ("color", {"mode": "fixed", "fixedColor": "red"}))]), 8, 8)
    return L.panels


# --------------------------------------------------------------------------- alert center
# Grafana as the alert sink for sites without mail/chat: everything is read from
# the ALERTS / ALERTS_FOR_STATE series Prometheus writes for every rule, so it
# needs no Alertmanager receiver and keeps history for the Prometheus retention.
AC = 'component=~"$scope", severity=~"$severity", namespace=~"$namespace"'


def rule_hints(rules_path):
    """alertname -> description with {{ $labels.x }} turned into <x>."""
    import re
    import yaml
    hints = {}
    for g in yaml.safe_load(open(rules_path))["spec"]["groups"]:
        for r in g["rules"]:
            d = r["annotations"].get("description", "")
            d = re.sub(r"\{\{\s*\$labels\.(\w+)\s*\}\}", r"<\1>", d)
            hints[r["alert"]] = re.sub(r"\{\{[^}]*\}\}", "…", d)
    return hints


def alert_center(hints):
    L = Layout()
    L.row("Now")
    fs = f'ALERTS{{{AC}, alertstate="firing"'
    L.add(stat("Critical", [tgt(f'count({fs}, severity="critical"}})', instant=True)],
               th=steps((None, "green"), (1, "red")), no_value="0"), 4, 4)
    L.add(stat("Warning", [tgt(f'count({fs}, severity="warning"}})', instant=True)],
               th=steps((None, "green"), (1, "orange")), no_value="0"), 4, 4)
    L.add(stat("Info", [tgt(f'count({fs}, severity="info"}})', instant=True)],
               th=steps((None, "green"), (1, "blue")), no_value="0"), 4, 4)
    L.add(stat("Pending", [tgt(f'count(ALERTS{{{AC}, alertstate="pending"}})', instant=True)],
               th=steps((None, "green"), (1, "yellow")), no_value="0",
               desc="Condition true, 'for' duration not yet reached. Becomes firing if it persists."), 4, 4)
    L.add(stat("Affected namespaces", [tgt(f'count(count by (namespace) ({fs}}}))', instant=True)],
               th=steps((None, "green"), (1, "orange")), no_value="0"), 4, 4)
    L.add(stat("Oldest active alert", [tgt(f'time() - min(ALERTS_FOR_STATE{{{AC}}})', instant=True)],
               unit="s", th=steps((None, "green"), (3600, "orange"), (86400, "red")), no_value="-",
               desc="How long the longest-running alert has been active."), 4, 4)

    L.add({"type": "alertlist", "title": "Alert inbox (expand an alert for summary / description)",
           "description": "Every firing/pending alert, whether Prometheus evaluates the rule (PrometheusRule) or Grafana does "
                          "(rules imported into Grafana). 'View alert rule' opens the rule with its query and history.",
           "options": {"viewMode": "list", "groupMode": "default", "groupBy": [], "maxItems": 100,
                       "sortOrder": 3, "dashboardAlerts": False, "alertName": "",
                       "alertInstanceLabelFilter": '{component=~"$scope", severity=~"$severity", namespace=~"$namespace"}',
                       # None = Grafana-managed AND data-source-managed rules, independent of the
                       # datasource name at the customer (a name here would hide imported rules).
                       "datasource": None, "folder": None, "showInstances": True,
                       "stateFilter": {"firing": True, "pending": True, "recovering": True,
                                       "noData": False, "normal": False, "error": True}}}, 24, 10)

    # ALERTS_FOR_STATE = activation time, but no alertstate label; ALERTS has the state.
    # group_right keeps the ALERTS label set (incl. alertstate) with the activation time as value.
    act = f'(ALERTS_FOR_STATE{{{AC}}} + ignoring (alertstate) group_right () (ALERTS{{{AC}}} * 0))'
    t = table("Active alerts", [
        tgt(act + " * 1000", ref="A", instant=True, fmt="table"),
        tgt(f"time() - {act}", ref="B", instant=True, fmt="table"),
        tgt(f'label_replace({act}, "action", "$1", "alertname", "(.*)")', ref="C", instant=True, fmt="table")],
        [{"id": "merge", "options": {}},
         # Rules add extra labels (slot_name, datname, ...); keep a fixed column set.
         {"id": "filterFieldsByName", "options": {"include": {"names": [
             "severity", "alertstate", "alertname", "namespace", "pod", "Value #A", "Value #B", "action"]}}},
         {"id": "organize", "options": {
             "excludeByName": {**ALERT_TABLE_HIDE, "Value #C": True,
                               "alertstate": False, "namespace": False, "Time": True},
             "renameByName": {"Value #A": "active since", "Value #B": "duration", "action": "what it means / what to do"},
             "indexByName": {"severity": 0, "alertstate": 1, "alertname": 2, "namespace": 3, "pod": 4,
                             "Value #A": 5, "Value #B": 6, "action": 7}}}],
        overrides=SEVERITY_COLORS + [
            by_name("alertstate", ("mappings", vmap({"firing": ("FIRING", "red"), "pending": ("pending", "yellow")})),
                    ("custom.cellOptions", {"type": "color-text"}), ("custom.width", 90)),
            by_name("severity", ("custom.width", 90)),
            by_name("alertname", ("custom.width", 260)),
            by_name("namespace", ("custom.width", 140)),
            by_name("pod", ("custom.width", 160)),
            by_name("active since", ("unit", "dateTimeAsIso"), ("custom.width", 170)),
            by_name("duration", ("unit", "s"), ("custom.width", 100)),
            by_name("what it means / what to do", ("mappings", vmap({k: (v, "text") for k, v in hints.items()})),
                    ("custom.cellOptions", {"type": "auto", "wrapText": True}))],
        desc="One row per active alert instance. 'active since' comes from ALERTS_FOR_STATE.",
        sort_by=[{"displayName": "duration", "desc": True}])
    t["options"]["cellHeight"] = "lg"
    L.add(t, 24, 10)

    L.row("History (selected time range)")
    L.add({"type": "state-timeline", "title": "Firing alerts over time",
           "targets": [tgt(f'max by (alertname, namespace, pod) ({fs}}})' + NO_ALERTS,
                           "{{alertname}} {{namespace}}/{{pod}}")],
           "fieldConfig": {"defaults": {"color": {"mode": "thresholds"},
                                        "thresholds": steps((None, "green"), (1, "red")),
                                        "mappings": TIMELINE_MAP,
                                        "custom": {"fillOpacity": 80, "lineWidth": 0}}, "overrides": []},
           "options": {"showValue": "never", "mergeValues": True, "rowHeight": 0.8, "alignValue": "left",
                       "legend": {"showLegend": False}, "tooltip": {"mode": "single"}}}, 24, 9)
    L.add(ts("Firing alerts by severity", [tgt(f'count by (severity) ({fs}}})', "{{severity}}")],
             bars=True, fill=80, stack=True,
             overrides=[by_name(s, ("color", {"mode": "fixed", "fixedColor": c}))
                        for s, c in (("critical", "red"), ("warning", "orange"), ("info", "blue"))]), 12, 8)
    L.add({"type": "bargauge", "title": "Top alerts by time firing",
           "targets": [tgt(f'topk(10, sum by (alertname) (count_over_time({fs}}}[$__range:1m])))',
                           "{{alertname}}", instant=True)],
           "description": "Sum over all instances of an alert, 1-minute resolution.",
           "fieldConfig": {"defaults": {"unit": "m", "color": {"mode": "thresholds"},
                                        "displayName": "${__field.labels.alertname}",
                                        "thresholds": steps((None, "orange"))}, "overrides": []},
           "options": {"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True,
                       "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}}, 12, 8)
    L.add(table("Alert log", [
        tgt(f'min_over_time(timestamp({fs}}})[$__range:1m]) * 1000', ref="A", instant=True, fmt="table"),
        tgt(f'max_over_time(timestamp({fs}}})[$__range:1m]) * 1000', ref="B", instant=True, fmt="table"),
        tgt(f'count_over_time({fs}}}[$__range:1m])', ref="C", instant=True, fmt="table"),
        tgt(f'{fs}}}', ref="D", instant=True, fmt="table")],
        [{"id": "merge", "options": {}},
         {"id": "filterFieldsByName", "options": {"include": {"names": [
             "severity", "Value #D", "alertname", "namespace", "pod", "Value #A", "Value #B", "Value #C"]}}},
         {"id": "organize", "options": {
             "excludeByName": {**ALERT_TABLE_HIDE, "namespace": False, "Time": True},
             "renameByName": {"Value #A": "first seen", "Value #B": "last seen",
                              "Value #C": "minutes firing", "Value #D": "status"},
             "indexByName": {"severity": 0, "Value #D": 1, "alertname": 2, "namespace": 3, "pod": 4,
                             "Value #A": 5, "Value #B": 6, "Value #C": 7}}}],
        overrides=SEVERITY_COLORS + [
            by_name("alertname", ("custom.width", 260)),
            by_name("first seen", ("unit", "dateTimeAsIso")),
            by_name("last seen", ("unit", "dateTimeAsIso")),
            by_name("minutes firing", ("unit", "m")),
            # Missing value = not firing now; noValue fills exactly those cells.
            by_name("status", ("mappings", vmap({1: ("FIRING", "red")})), ("noValue", "resolved"),
                    ("custom.cellOptions", {"type": "color-text"}))],
        desc="Every alert instance that fired in the selected range (1-minute resolution). Range limited by Prometheus retention.",
        sort_by=[{"displayName": "last seen", "desc": True}]), 24, 10)
    return L.panels


def alert_center_vars():
    def custom(name, label, pairs, multi, all_value=None):
        opts = [{"text": t, "value": v, "selected": i == 0} for i, (t, v) in enumerate(pairs)]
        return {"name": name, "label": label, "type": "custom", "hide": 0,
                "query": ",".join(f"{t} : {v}" for t, v in pairs), "options": opts,
                "current": {"text": opts[0]["text"], "value": opts[0]["value"]},
                "multi": multi, "includeAll": all_value is not None, "allValue": all_value}
    sev = custom("severity", "Severity", [("critical", "critical"), ("warning", "warning"), ("info", "info")],
                 True, ".*")
    sev["current"] = {"text": ["All"], "value": ["$__all"]}
    return {"list": [
        {"name": "datasource", "label": "Datasource", "type": "datasource", "query": "prometheus",
         "current": {}, "hide": 0, "options": [], "refresh": 1},
        custom("scope", "Scope", [("EDB Postgres", "edb-postgres"), ("All alerts", ".*")], False),
        {"name": "namespace", "label": "Namespace", "type": "query", "datasource": DS,
         "query": {"query": "label_values(ALERTS_FOR_STATE, namespace)",
                   "refId": "PrometheusVariableQueryEditor-VariableQuery"},
         "definition": "label_values(ALERTS_FOR_STATE, namespace)", "refresh": 2, "sort": 1, "hide": 0,
         "includeAll": True, "allValue": ".*", "multi": True,
         "current": {"text": ["All"], "value": ["$__all"]}, "options": []},
        sev]}


def main(out_dir):
    ov = dashboard("edb-pg-overview", "EDB PostgreSQL — Cluster Overview",
                   ["edb", "postgres", "postgresql", "cloudnative-pg", "database"], overview(),
                   [link("Backups", "edb-pg-backups", "cloud"), link("Alert Center", "edb-alert-center", "bolt")],
                   "now-6h", "1m", variables())
    bk = dashboard("edb-pg-backups", "EDB PostgreSQL — Backups",
                   ["edb", "postgres", "cloudnative-pg", "backup"], backups(),
                   [link("Cluster Overview", "edb-pg-overview"), link("Alert Center", "edb-alert-center", "bolt")],
                   "now-7d", "5m", variables())
    ac = dashboard("edb-alert-center", "EDB PostgreSQL — Alert Center",
                   ["edb", "postgres", "alerts"], alert_center(rule_hints(f"{out_dir}/../manifests/edb-postgres-alerts.yaml")),
                   [link("Cluster Overview", "edb-pg-overview")], "now-24h", "30s", alert_center_vars())
    ac["annotations"]["list"] = ac["annotations"]["list"][:1]  # restart annotation needs $job, not defined here
    tooltips.apply(ov, tooltips.OVERVIEW_PANELS, tooltips.OVERVIEW_COLUMNS, tooltips.VARS_EN)
    tooltips.apply(bk, tooltips.BACKUP_PANELS, tooltips.BACKUP_COLUMNS, tooltips.VARS_DE)
    tooltips.apply(ac, tooltips.ALERT_PANELS, tooltips.ALERT_COLUMNS, tooltips.VARS_ALERTS)
    for name, d in [("dashboard-edb.json", ov), ("dashboard-edb-backups.json", bk),
                    ("dashboard-edb-alerts.json", ac)]:
        with open(f"{out_dir}/{name}", "w") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
            f.write("\n")


if __name__ == "__main__":
    main(sys.argv[1])
