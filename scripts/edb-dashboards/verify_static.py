import json, re, sys, yaml

REPO, EDB = sys.argv[1], sys.argv[2]
errors = []

# Known metric names: default-monitoring.yaml + documented cnp_collector_* + Prometheus built-ins.
dm = yaml.safe_load(open(f"{EDB}/default-monitoring.yaml"))
q = yaml.safe_load(dm["data"]["queries"])
known = {"ALERTS", "ALERTS_FOR_STATE", "up", "time"}
for name, v in q.items():
    name = v.get("name", name)
    for m in v["metrics"]:
        (col, c), = m.items()
        if c["usage"] not in ("LABEL", "DISCARD"):
            known.add(f"cnp_{name}_{c.get('name', col)}")
known |= {f"cnp_collector_{n}" for n in (
    "up fencing_on nodes_used last_collection_error manual_switchover_required pg_wal "
    "pg_wal_archive_status replica_mode sync_replicas postgres_version last_failed_backup_timestamp "
    "last_available_backup_timestamp first_recoverability_point wal_bytes wal_records wal_fpi").split()}

FUNCS = {"sum", "max", "min", "count", "rate", "increase", "changes", "time", "by", "on", "and",
         "or", "unless", "ignoring", "bool", "group_left", "abs", "timestamp", "min_over_time",
         "max_over_time", "count_over_time", "topk", "label_replace", "group_right", "vector"}
LABELS = {"namespace", "job", "pod", "value", "name", "state", "datname", "slot_name",
          "application_name", "full", "alertname", "severity", "alertstate", "component",
          "usename", "container", "endpoint", "extname", "installed_version", "default_version",
          "__name__", "cluster", "action"}


def check_expr(where, e):
    s = re.sub(r'"[^"]*"', '""', e)  # strip string literals
    for o, c in ("()", "{}", "[]"):
        if s.count(o) != s.count(c):
            errors.append(f"{where}: unbalanced {o}{c}: {e}")
    if e.count('"') % 2:
        errors.append(f"{where}: odd quotes")
    s = re.sub(r"\[[^\]]*\]", "", s)  # range selectors
    for ident in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", s):
        if ident in FUNCS or ident in LABELS or ident in known:
            continue
        if ident.startswith("__rate"):
            continue
        errors.append(f"{where}: unknown identifier '{ident}'")
    for lbl in re.findall(r"by \(([^)]*)\)", e):
        for l in lbl.split(","):
            if l.strip() not in LABELS:
                errors.append(f"{where}: unknown by-label {l}")


# ---- dashboards
dash_alert_res = []
for f in ["dashboard-edb.json", "dashboard-edb-backups.json", "dashboard-edb-alerts.json"]:
    d = json.load(open(f"{REPO}/dashboards/{f}"))
    vars_ = {v["name"] for v in d["templating"]["list"]} | {"__rate_interval", "__url_time_range", "__range"}
    ids, cells = set(), {}
    for p in d["panels"]:
        if p["id"] in ids:
            errors.append(f"{f}: dup id {p['id']}")
        ids.add(p["id"])
        g = p["gridPos"]
        if g["x"] + g["w"] > 24:
            errors.append(f"{f}: '{p['title']}' exceeds width")
        for x in range(g["x"], g["x"] + g["w"]):
            for y in range(g["y"], g["y"] + g["h"]):
                if (x, y) in cells:
                    errors.append(f"{f}: '{p['title']}' overlaps '{cells[(x, y)]}'")
                    break
                cells[(x, y)] = p["title"]
        for t in p.get("targets", []):
            e = t["expr"]
            for v in re.findall(r"\$\{?([A-Za-z_]\w*)", e):
                if v not in vars_:
                    errors.append(f"{f}/{p['title']}: undefined var ${v}")
            check_expr(f"{f}/{p['title']}", e.replace("$__rate_interval", "5m"))
            dash_alert_res += re.findall(r'alertname=~"([^"]+)"', e)
    # Tooltips: every panel, variable, link and visible table column must explain itself.
    for p in d["panels"]:
        if p["type"] == "row":
            continue
        if not p.get("description", "").strip():
            errors.append(f"{f}: panel '{p['title']}' has no tooltip (description)")
        if p["type"] == "table":
            org = next((t["options"] for t in p.get("transformations", []) if t["id"] == "organize"), {})
            inc = next((t["options"]["include"]["names"] for t in p.get("transformations", [])
                        if t["id"] == "filterFieldsByName"), [])
            ren, exc = org.get("renameByName", {}), org.get("excludeByName", {})
            cols = {ren.get(k, k) for k in set(inc) | set(org.get("indexByName", {})) | set(ren) if not exc.get(k)}
            # Column help lives in the panel tooltip as "- **<column>**: ..." (headers can't show it).
            for c in sorted(c for c in cols if f"**{c}**" not in p.get("description", "")):
                errors.append(f"{f}: table '{p['title']}' column '{c}' not explained in panel tooltip")
    for v in d["templating"]["list"]:
        if not v.get("description", "").strip():
            errors.append(f"{f}: variable '{v['name']}' has no tooltip (description)")
    for lk in d["links"]:
        if not lk.get("tooltip", "").strip():
            errors.append(f"{f}: link '{lk['title']}' has no tooltip")
    for a in d["annotations"]["list"]:
        if "expr" in a:
            check_expr(f"{f}/annotation", a["expr"])
    print(f"{f}: {len(d['panels'])} panels, {sum(len(p.get('targets', [])) for p in d['panels'])} queries")

# ---- rules
r = yaml.safe_load(open(f"{REPO}/manifests/edb-postgres-alerts.yaml"))
names = []
for g in r["spec"]["groups"]:
    for rule in g["rules"]:
        n = rule["alert"]
        names.append(n)
        check_expr(n, rule["expr"])
        if rule["labels"].get("component") != "edb-postgres":
            errors.append(f"{n}: missing component label")
        if rule["labels"].get("severity") not in ("critical", "warning", "info"):
            errors.append(f"{n}: bad severity")
        if "for" in rule and not re.fullmatch(r"\d+[smh]", rule["for"]):
            errors.append(f"{n}: bad for")
        # labels used in annotations must survive the expression
        by = set()
        for grp in re.findall(r"by \(([^)]*)\)", rule["expr"]):
            by |= {x.strip() for x in grp.split(",")}
        aggregated = bool(re.match(r"\s*(time\(\) - )?(sum|max|min|count) by", rule["expr"]))
        for lbl in re.findall(r"\$labels\.(\w+)", json.dumps(rule["annotations"])):
            if aggregated and lbl not in by:
                errors.append(f"{n}: annotation uses $labels.{lbl} dropped by aggregation")
        # every alert must keep job+namespace for the dashboard filter
        if aggregated and not {"namespace", "job"} <= by:
            errors.append(f"{n}: aggregation drops namespace/job -> invisible on dashboard")
if len(names) != len(set(names)):
    errors.append("duplicate alert names")
for rx in dash_alert_res:
    if not any(re.fullmatch(rx, n) for n in names):
        errors.append(f"dashboard regex {rx} matches no rule")
    print(f"backup-dashboard alert filter {rx} matches:", [n for n in names if re.fullmatch(rx, n)])
print(f"rules: {len(names)} alerts")

print("\n".join(errors) if errors else "OK: no errors")
sys.exit(1 if errors else 0)
