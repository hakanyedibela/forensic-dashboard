"""Run every dashboard query and variable against a live Prometheus."""
import json, os, sys, urllib.parse, urllib.request

PROM = os.environ.get("PROM_URL", "http://localhost:9090")
VARS = {"namespace": os.environ.get("NS", "edb-lab"), "cluster": os.environ.get("CLUSTER", "pg-lab"),
        "job": os.environ.get("JOB", "edb-lab/pg-lab-metrics"),
        "__rate_interval": "2m", "__range": "24h",
        "scope": os.environ.get("SCOPE", ".*"), "severity": ".*"}


def q(expr):
    data = urllib.parse.urlencode({"query": expr}).encode()
    try:
        r = json.load(urllib.request.urlopen(f"{PROM}/api/v1/query", data))
        return r["data"]["result"], None
    except urllib.error.HTTPError as e:
        return None, json.load(e).get("error")


def sub(e):
    for k in sorted(VARS, key=len, reverse=True):
        e = e.replace("${" + k + "}", VARS[k]).replace("$" + k, VARS[k])
    return e


bad = 0
for f in sys.argv[1:]:
    d = json.load(open(f))
    print(f"\n=== {d['title']}")
    for v in d["templating"]["list"]:
        if v["type"] == "query":
            m = v["definition"][len("label_values("):-1]
            if "," not in m:
                continue
            sel, lbl = m.rsplit(",", 1)
            res, err = q(f"count by ({lbl.strip()}) ({sub(sel)})")
            vals = [r["metric"].get(lbl.strip()) for r in res or []]
            print(f"  var {v['name']:10} -> {vals} {err or ''}")
    for p in d["panels"]:
        for t in p.get("targets", []):
            res, err = q(sub(t["expr"]))
            if err:
                status, bad = f"ERROR {err}", bad + 1
            elif not res:
                status = "EMPTY"
            else:
                vals = [r["value"][1] for r in res][:3]
                status = f"{len(res)} series, e.g. {vals}"
            print(f"  [{t['refId']}] {p['title'][:42]:42} {status}")
    for a in d["annotations"]["list"]:
        if "expr" in a:
            res, err = q(sub(a["expr"]))
            print(f"  annotation {a['name']}: {'ERROR ' + err if err else 'ok'}")
            bad += bool(err)
print(f"\nquery errors: {bad}")
sys.exit(bool(bad))
