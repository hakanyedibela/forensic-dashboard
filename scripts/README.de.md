# Scripts

Hilfsskripte für die Arbeit mit dem OOM-Observability-Stack auf der Kommandozeile. Jedes Skript ist ein eigenständiges Python-3-Programm (ohne Drittanbieter-Abhängigkeiten) und funktioniert sowohl mit `oc` als auch mit `kubectl`.

> Englische Version: [`README.md`](./README.md)

## Übersicht

| Skript | Zweck |
|---|---|
| [`oom-extract.py`](#oom-extractpy) | Listet aktuell OOMKilled-Container in einem Namespace (oder allen) mit Workload, Node, Limits und passenden Kubernetes-Events. |
| [`oom-logs.py`](#oom-logspy) | Gibt die **Pre-OOM**-Logs (und optional aktuelle / Sibling-Logs) jedes OOMKilled-Containers in einem Namespace aus. |
| [`oom-usage.py`](#oom-usagepy) | Rekonstruiert **Memory- und CPU-Verbrauch zum OOM-Zeitpunkt** über Prometheus/Thanos für jeden OOMKilled-Container. |

---

## `oom-extract.py`

Extrahiert OOMKilled-Informationen für Pods in einem Namespace, indem die Kubernetes-/OpenShift-API über `oc` (Standard) oder `kubectl` abgefragt wird.

Für jeden Container, dessen **letzter Terminierungsgrund** `OOMKilled` war, werden folgende Werte ausgegeben:

| Spalte | Quelle |
|---|---|
| `NAMESPACE` | `pod.metadata.namespace` |
| `WORKLOAD` | `pod.metadata.ownerReferences` (ReplicaSet → Deployment wird automatisch aufgelöst) |
| `POD` | `pod.metadata.name` |
| `CONTAINER` | `containerStatuses[].name` |
| `NODE` | `pod.spec.nodeName` |
| `RESTARTS` | `containerStatuses[].restartCount` |
| `EXIT` | `lastState.terminated.exitCode` (137 bei OOM) |
| `OOM AGE` | `lastState.terminated.finishedAt` |
| `MEM LIMIT` | `pod.spec.containers[].resources.limits.memory` |
| `MEM REQ` | `pod.spec.containers[].resources.requests.memory` |
| `K8S EVENT` | jüngstes Event mit `reason=OOMKilling` für den Pod |

### Voraussetzungen

- Python 3.8+
- `oc` (oder `kubectl` mit `--kubectl`)
- Aktive Session (`oc login` / `kubectl config use-context …`)
- RBAC: `get pods`, `get replicasets`, `get events` im Ziel-Namespace

### Verwendung

```bash
# aktuelles oc-Projekt
./oom-extract.py

# bestimmter Namespace
./oom-extract.py -n my-app

# alle Namespaces (clusterweiter Scan)
./oom-extract.py -A

# JSON-Ausgabe (für jq, zur Weiterverarbeitung etc.)
./oom-extract.py -A --json

# Event-Lookup überspringen (schneller auf großen Clustern)
./oom-extract.py -A --no-events

# kubectl statt oc verwenden
./oom-extract.py --kubectl -n my-app
```

### Beispielausgabe

```
# OOMKilled containers in namespace oom-test (lastState.terminated.reason)
NAMESPACE  WORKLOAD            POD                          CONTAINER   NODE       RESTARTS  EXIT  OOM AGE   MEM LIMIT  MEM REQ  K8S EVENT
oom-test   Deployment/leak-a   leak-a-6f7c9b8d57-x4f2g      app         worker-2   12        137   3m ago    256.0Mi    128.0Mi  3m ago
oom-test   Deployment/spike-b  spike-b-7d4b5c6789-q9k2p     app         worker-1   2         137   42m ago   512.0Mi    256.0Mi  -
oom-test   StatefulSet/db      db-0                         postgres    worker-3   1         137   1h ago    1.0Gi      512.0Mi  -
```

### Optionen

| Flag | Beschreibung |
|---|---|
| `-n, --namespace NS` | Bestimmter Namespace (Standard: aktuelles `oc project`). |
| `-A, --all-namespaces` | Scannt alle Namespaces. Schließt `-n` aus. |
| `--json` | Gibt ein JSON-Array statt einer Texttabelle aus. |
| `--no-events` | Überspringt den Lookup der `OOMKilling`-Events (ein API-Call weniger). |
| `--kubectl` | Verwendet `kubectl` statt `oc`. |

### Wie der Workload aufgelöst wird

Das Skript folgt `pod.metadata.ownerReferences`:

- **Deployment** — Owner ist ein `ReplicaSet`; das Skript holt einmalig alle ReplicaSets im Scope per Bulk-Call und folgt deren Owner-Reference zum übergeordneten Deployment.
- **StatefulSet / DaemonSet / Job / CronJob** — Owner wird direkt übernommen.
- **Standalone-Pod** — Spalte zeigt `-`.

### Was dieses Skript *nicht* tut

- Es sieht **keine gelöschten Pods**. Die Kubernetes-API liefert nur aktuell existierende Pods, daher tauchen Pods, die nach ihrem OOM entfernt wurden (z. B. durch Job-Cleanup oder manuelles `oc delete`), hier nicht auf. Für eine 7-Tage-Historie aus Prometheus das Grafana-Dashboard **OOMKilled — 7d Detail + Drilldown** (uid `oom-7d-detail`) verwenden.
- Es **korreliert keine Logs**. Dafür Loki / das Grafana-Drilldown nutzen.
- Es liest **kein Kernel-`dmesg`**. Für Node-Level-OOMs (System-OOM vs. cgroup-OOM) ist `ssh <node> "sudo dmesg -T | grep -i 'killed process'"` weiterhin die maßgebliche Quelle.
- Die Spalte `K8S EVENT` ist Best-Effort: Kubernetes-Events werden typischerweise nur ~1 Stunde aufbewahrt, ältere OOMs zeigen daher `-`, auch wenn `lastState` noch OOMKilled meldet.

### Exit Codes

- `0` — fertig (Ergebnismenge darf leer sein)
- `2` — `oc` / `kubectl` nicht im `PATH`
- nicht null — der zugrundeliegende CLI-Befehl ist fehlgeschlagen (stderr wird durchgereicht)

---

## `oom-logs.py`

Findet dieselben OOMKilled-Container wie `oom-extract.py` und gibt für jeden den Container-Log über `oc logs … --previous` aus (die Logzeilen des gekillten Prozesses — meist das wichtigste forensische Artefakt).

Standardverhalten: nur Pre-OOM-Logs, letzte 200 Zeilen pro Container, aktuelles `oc project`, Ausgabe nach stdout mit klarem Header pro (Pod, Container).

### Was abgerufen wird

| Abschnitt | Wann ausgegeben | Quelle |
|---|---|---|
| `PREVIOUS (pre-OOM)` | immer (außer mit `--no-previous`) | `oc logs <pod> -c <ctr> --previous` |
| `CURRENT` | mit `--current` | `oc logs <pod> -c <ctr>` |
| `SIBLING (live workload peer)` | mit `--include-siblings` | `oc logs <other-pod> -c <ctr>` für jeden weiteren laufenden Pod desselben Deployments/StatefulSets/DaemonSets |

Jedem Abschnitt geht eine fixe `===`-Headerzeile voraus mit Namespace, Pod, Container, Workload, OOM-Zeitstempel, Exit-Code und Restart-Count.

### Voraussetzungen

- Python 3.8+
- `oc` (oder `kubectl` mit `--kubectl`)
- Aktive Session (`oc login`)
- RBAC: `get pods`, `get replicasets`, `get pods/log` im Ziel-Namespace

### Verwendung

```bash
# Pre-OOM-Logs für jeden OOMKilled-Container im aktuellen Namespace
./oom-logs.py

# bestimmter Namespace, letzte 500 Zeilen
./oom-logs.py -n my-app --tail 500

# alle OOMs clusterweit, nur Zeilen, die nach Fehlern aussehen
./oom-logs.py -A --grep "out of memory|OutOfMemoryError|fatal|panic"

# zusätzlich aktuellen Container-Log (nach Restart) und Live-Siblings einbeziehen
./oom-logs.py -n my-app --current --include-siblings

# alle Logzeilen (kein Tail) seit der letzten Stunde
./oom-logs.py -n my-app --tail 0 --since 1h

# eine Datei pro (namespace,pod,container) in ./oom-logs/ schreiben
./oom-logs.py -A --output-dir ./oom-logs

# nur betroffene Pods auflisten, keine Logs abrufen
./oom-logs.py -A --list
```

### Beispielausgabe

```
================================================================================
# PREVIOUS (pre-OOM): oom-test/leak-a-6f7c9b8d57-x4f2g  container=app
# workload=Deployment/leak-a  oomed_at=2026-05-04T08:12:33Z  exit=137  restarts=12
================================================================================
2026-05-04T08:12:30.881Z INFO  serving request id=abc123 size=82MiB
2026-05-04T08:12:31.402Z WARN  GC pause 940ms
2026-05-04T08:12:32.119Z ERROR java.lang.OutOfMemoryError: Java heap space
        at com.example.Worker.handle(Worker.java:142)
        ...
```

### Optionen

| Flag | Beschreibung |
|---|---|
| `-n, --namespace NS` | Bestimmter Namespace (Standard: aktuelles `oc project`). |
| `-A, --all-namespaces` | Scannt alle Namespaces. Schließt `-n` aus. |
| `--tail N` | Zeilen pro Abschnitt. `0` = alles. Standard `200`. |
| `--since DURATION` | Wird an `oc logs --since` weitergereicht, z. B. `1h`, `24h`, `7d`. |
| `--current` | Gibt zusätzlich den aktuellen (Post-Restart) Container-Log aus. |
| `--no-previous` | Überspringt den `--previous`-Abschnitt (selten — meist will man ihn). |
| `--include-siblings` | Holt für jedes betroffene Workload zusätzlich aktuelle Logs der weiteren laufenden Pods (nützlich für „Trifft das alle Replicas oder nur einen?"). |
| `--grep PATTERN` | Case-insensitiver Regex pro Zeile. Jeder Abschnitt behält nur passende Zeilen (und meldet, wenn keine matcht). |
| `--output-dir DIR` | Schreibt eine Datei pro (namespace,pod,container) nach `DIR` statt nach stdout. Dateinamen: `<ns>__<pod>__<container>.log`. |
| `--list` | Listet nur betroffene Pods (TSV), ruft `oc logs` nicht auf. |
| `--kubectl` | Verwendet `kubectl` statt `oc`. |

### Hinweise zum Verhalten

- `oc logs --previous` schlägt legitim fehl, wenn ein Container nie neu gestartet wurde. Das Skript fängt das ab, schreibt einen `[no logs available — oc exit N]`-Platzhalter für den betroffenen Abschnitt und macht mit dem nächsten Ziel weiter. Der Gesamtlauf bricht **nicht** ab.
- Es gelten dieselben Einschränkungen wie bei `oom-extract.py`: gelöschte Pods sind unsichtbar, und es werden nur Container erfasst, deren `lastState.terminated.reason == "OOMKilled"` ist. Für eine 7-Tage-Historie das Grafana-Drilldown (`oom-7d-detail`) und das Loki-Panel verwenden.
- `--include-siblings` setzt einen `get pods`-Call pro betroffenem Workload und einen `oc logs`-Call pro Sibling ab. Auf großen Clustern mit vielen OOM-Workloads vervielfacht das die API-Last — Default daher aus.
- Ausgabe ist Plaintext und pipe-tauglich (keine Farb-Codes), `./oom-logs.py -A | less` und `./oom-logs.py -A | grep -i 'oom'` funktionieren wie erwartet.

### Exit Codes

- `0` — fertig (Ausgabe darf leer sein, wenn nichts OOMKilled wurde)
- `2` — `oc` / `kubectl` nicht im `PATH`
- nicht null — ein nicht-`logs`-Befehl ist fehlgeschlagen (`get pods`, `get rs`); stderr wird durchgereicht

---

## `oom-usage.py`

Beantwortet die Frage **„Wie viel Memory und CPU hat der Container tatsächlich verbraucht, als er gekillt wurde?"** Dieser Wert steht *nicht* in der Kubernetes-API — `lastState.terminated` enthält nur Grund und Exit-Code. Er steht *in Prometheus*, das cAdvisor alle ~30 s abfragt.

Das Skript ermittelt die OOMKilled-Container (gleiche Logik wie `oom-extract.py`), liest `lastState.terminated.finishedAt` als OOM-Zeitstempel und führt für jeden Container eine Instant-Query gegen Prometheus zu diesem Zeitpunkt aus.

### Abgefragte Metriken

Pro Spalte werden die folgenden Ausdrücke der Reihe nach probiert; der **erste, der einen Wert liefert**, gewinnt. Hat dein Prometheus also nur die Recording-Rule-Aliase oder nur die alte KSM-Metrik, füllt sich die Tabelle trotzdem.

| Spalte | PromQL-Fallback-Kette |
|---|---|
| `WSS@OOM` | `container_memory_working_set_bytes{…}` → `node_namespace_pod_container:container_memory_working_set_bytes{…}` |
| `WSS PEAK` | `max_over_time(container_memory_working_set_bytes{…}[<window>])` → `max_over_time(node_namespace_pod_container:container_memory_working_set_bytes{…}[<window>])` |
| `RSS@OOM` | `container_memory_rss{…}` → `node_namespace_pod_container:container_memory_rss{…}` |
| `LIMIT` | `kube_pod_container_resource_limits{…,resource="memory"}` → `kube_pod_container_resource_limits_memory_bytes{…}` (Legacy KSM) → `container_spec_memory_limit_bytes{…}` |
| `% LIMIT` | `WSS PEAK / LIMIT` × 100 |
| `CPU@OOM(cores)` | `rate(container_cpu_usage_seconds_total{…}[<window>])` → `node_namespace_pod_container:container_cpu_usage_seconds_total:sum_irate{…}` → `…:sum_rate{…}` |

Alle Queries werden zum Zeitpunkt `t = OOM-Zeit` (`lastState.terminated.finishedAt`) ausgewertet, mit exakt fixierten namespace/pod/container-Labels. Standard-`<window>` ist `5m` (`--window`).

`working_set` ist die Metrik, anhand derer der Kernel-OOM-Killer entscheidet — `WSS PEAK / LIMIT` nahe oder über 100 % ist also der Smoking-Gun-Beweis für einen Limit-getriebenen Kill. Liegt der Peak deutlich *unter* dem Limit (z. B. 40 %) und es gibt trotzdem einen echten OOM, kam der Kill durch Node-Memory-Druck und nicht durch das cgroup-Limit — andere Ursache, andere Maßnahme.

### Diese Metriken nicht vorhanden? `--diagnose` verwenden

Wenn Spalten als `-` zurückkommen, obwohl Prometheus erreichbar ist, exportieren deine Scrape-Jobs vermutlich nicht die Namen, die das Skript erwartet (häufig bei abgespeckten Monitoring-Stacks, eigenen Relabel-Regeln oder stark gefilterten Thanos-Receivern).

`--diagnose` führt für jede bekannte Variante `count(<metric>)` aus und meldet, welche existieren:

```bash
./oom-usage.py --diagnose                      # lokales Prometheus
./oom-usage.py --port-forward --diagnose       # Auto-Tunnel + Probe
```

Beispielausgabe:

```
Probing Prometheus at http://127.0.0.1:9090 for known OOM-relevant metrics.
An 'OK' line means the metric exists and returns at least one series.
Use the OK names below; the script will pick the first OK variant per kind.

  container_memory_working_set_bytes                                      OK (5234 series)         # Memory working set (cAdvisor)
  node_namespace_pod_container:container_memory_working_set_bytes         missing                  # Memory working set (recording rule)
  container_memory_rss                                                    missing                  # Memory RSS (cAdvisor)
  node_namespace_pod_container:container_memory_rss                       OK (5234 series)         # Memory RSS (recording rule)
  kube_pod_container_resource_limits{resource="memory"}                   OK (1024 series)         # Memory limit (kube-state-metrics)
  kube_pod_container_resource_limits_memory_bytes                         missing                  # Memory limit (KSM legacy)
  container_spec_memory_limit_bytes                                       OK (5234 series)         # Memory limit (cAdvisor)
  container_cpu_usage_seconds_total                                       OK (5234 series)         # CPU usage counter (cAdvisor)
  node_namespace_pod_container:container_cpu_usage_seconds_total:sum_irate  missing                # CPU usage (recording, irate)
  container_oom_events_total                                              OK (1024 series)         # OOM event counter (cAdvisor)
  kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}    OK (12 series)           # Pod terminated reason (KSM)
```

So liest man das:
- **Alles `missing`** → kein cAdvisor-/kube-state-metrics-Scrape-Job vorhanden. Auch die Grafana-Dashboards in diesem Repo funktionieren dann nicht. Fix: `kube-prometheus-stack` deployen (siehe `install.sh` in diesem Repo) oder `kubelet/cadvisor` und `kube-state-metrics` scrapen.
- **cAdvisor-Namen fehlen, aber Recording-Rules `OK`** → typisches kube-prometheus-stack-Setup mit Allowlist. Die Fallbacks des Skripts greifen automatisch — ohne `--diagnose` neu starten und die Tabelle sollte sich füllen.
- **`kube_pod_container_status_last_terminated_reason` fehlt** → kube-state-metrics ist nicht deployed. OOM-Detection per Prometheus ist dann unmöglich; `oom-extract.py` und `oom-logs.py` (lesen direkt aus der API) funktionieren weiter, aber das Dashboard `oom-7d-detail` und die `LIMIT`-Spalte hier bleiben leer.

### Voraussetzungen

- Python 3.8+ (nur Standardbibliothek — verwendet `urllib`, kein `requests`)
- `oc` (oder `kubectl` mit `--kubectl`) für die Pod-Discovery
- Erreichbarer Prometheus- / Thanos-HTTP-Endpunkt
- Für OpenShift Thanos: ein Bearer-Token (wird automatisch über `oc whoami -t` geholt, falls nicht angegeben)

### Prometheus erreichen

Vier häufige Muster:

```bash
# 1. Auto-Port-Forward (am einfachsten — Skript findet den Service und tunnelt selbst)
./oom-usage.py --port-forward                           # probiert kps + openshift-monitoring

# 2. Manuelles Port-Forward auf kube-prometheus-stack (passt zur Installation in diesem Repo)
oc -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090
./oom-usage.py                                          # Default-URL ist http://localhost:9090

# 3. OpenShift-Thanos-Querier (Produktiv-OpenShift-Cluster)
./oom-usage.py \
  --prometheus-url "https://$(oc -n openshift-monitoring get route thanos-querier -o jsonpath='{.spec.host}')" \
  --insecure                                            # Token wird automatisch via `oc whoami -t` geholt

# 4. Eigener Prometheus mit explizitem Bearer-Token
./oom-usage.py --prometheus-url https://prom.internal --token "$MY_TOKEN"
```

Ist die URL nicht erreichbar, bricht das Skript mit **einer** verständlichen Meldung (Exit-Code `3`) ab — keine Wiederholung desselben Connection-Errors pro PromQL-Ausdruck und pro Pod mehr.

Discovery-Reihenfolge bei `--port-forward`:

| # | Namespace | Service | Port |
|---|---|---|---|
| 1 | `monitoring` | `kps-kube-prometheus-stack-prometheus` | 9090 |
| 2 | `monitoring` | `prometheus-operated` | 9090 |
| 3 | `openshift-monitoring` | `thanos-querier` | 9091 |
| 4 | `openshift-monitoring` | `prometheus-k8s` | 9090 |

Der erste vorhandene Service gewinnt. Lokalen Port mit `--local-port N` ändern, falls 9090 belegt ist. Das Port-Forward wird beim Skript-Ende automatisch beendet.

### Verwendung

```bash
# aktueller Namespace, 5-Minuten-Peak-Fenster (Default)
./oom-usage.py

# bestimmter Namespace
./oom-usage.py -n my-app

# alle Namespaces, 15 Minuten zurückblicken (für langsame Leaks)
./oom-usage.py -A --window 15m

# maschinenlesbare JSON-Ausgabe
./oom-usage.py -A --json
```

### Beispielausgabe

```
# OOM usage for namespace oom-test via http://localhost:9090 (lookback=5m)
NAMESPACE  WORKLOAD            POD                          CONTAINER  OOM AT                WSS@OOM   WSS PEAK  RSS@OOM   LIMIT     % LIMIT  CPU@OOM(cores)
oom-test   Deployment/leak-a   leak-a-6f7c9b8d57-x4f2g      app        2026-05-04T08:12:33Z  248.4Mi   255.9Mi   240.1Mi   256.0Mi   99.9%    0.420
oom-test   Deployment/spike-b  spike-b-7d4b5c6789-q9k2p     app        2026-05-04T07:30:11Z  410.7Mi   511.3Mi   402.0Mi   512.0Mi   99.9%    1.230
oom-test   StatefulSet/db      db-0                         postgres   2026-05-04T05:02:48Z  430.0Mi   480.5Mi   420.0Mi   1.0Gi     46.9%    0.090
```

Die `db-0`-Zeile oben (% LIMIT = 47 %) ist die typische Signatur eines **Node-Pressure**-OOM (kein cgroup-Limit-OOM) — der Container war weit von seinem eigenen Limit entfernt.

### Optionen

| Flag | Beschreibung |
|---|---|
| `-n, --namespace NS` | Bestimmter Namespace (Standard: aktuelles `oc project`). |
| `-A, --all-namespaces` | Scannt alle Namespaces. |
| `--prometheus-url URL` | Prometheus- / Thanos-Basis-URL. Standard: `http://localhost:9090`. |
| `--token TOKEN` | Bearer-Token. Default: `oc whoami -t`, falls verfügbar. |
| `--insecure` | TLS-Validierung überspringen (Self-signed Prometheus, OpenShift-Route mit interner CA). |
| `--port-forward` | Findet einen Prometheus-Service und startet `oc port-forward` automatisch; wird beim Beenden abgebaut. Überschreibt `--prometheus-url`. |
| `--local-port N` | Lokaler Port für `--port-forward` (Standard `9090`). Anderen Wert verwenden, wenn 9090 belegt ist. |
| `--window DURATION` | PromQL-Range für `max_over_time` und `rate`. Standard `5m`. Für langsame Leaks erhöhen. |
| `--diagnose` | Probt jede bekannte OOM-relevante Metrik-Variante in Prometheus, gibt aus welche Daten liefern, und beendet sich. Hilfreich, wenn Verbrauchsspalten als `-` zurückkommen. |
| `--json` | JSON-Array, eine Zeile pro OOMKilled-Container, mit Roh-Werten in Bytes/Cores. |
| `--kubectl` | Verwendet `kubectl` statt `oc` für die Pod-Discovery. |

### Hinweise zum Verhalten

- Ein `-` in einer Verbrauchsspalte bedeutet, dass Prometheus kein Sample für diesen Selector zu diesem Zeitpunkt geliefert hat. Häufige Ursachen: OOM älter als die Prometheus-Retention, falsche URL/Token oder cAdvisor-Labels passen nicht (z. B. Cluster mit nicht-Standard-Relabel).
- Pro fehlgeschlagener Query wird eine Warnzeile auf stderr geschrieben und das Skript läuft weiter — ein einzelnes fehlendes Sample bricht die ganze Tabelle nicht ab.
- Das Lookback-Fenster `--window` steuert *sowohl* den Peak (`max_over_time`) *als auch* die CPU-Rate (`rate`). Für chronische Leaks, die über Stunden gewachsen sind, auf `1h` oder `6h` erhöhen.
- Das Skript dedupliziert nicht: Hat ein Pod mehrere OOMKilled-Container, gibt es eine Zeile pro Container (gewünscht für Sidecar-lastige Workloads).
- Kernel-Level-Bestätigung (`dmesg` "Killed process N (java) total-vm:… anon-rss:…") wird hier nicht abgeholt. Das ist die genaueste Einzelzahl, erfordert aber `oc debug node/<node>` und Root-Rechte auf dem Node — außerhalb des Scopes für ein Routinen-Extraktions-Skript.

### Exit Codes

- `0` — fertig
- `2` — `oc` / `kubectl` nicht im `PATH`
- `3` — Prometheus nicht erreichbar oder `--port-forward` konnte keinen Tunnel öffnen
- nicht null — ein nicht-Prometheus-CLI-Befehl ist fehlgeschlagen; stderr wird durchgereicht

---

## Ein neues Skript hinzufügen

Konventionen für Erweiterungen in diesem Verzeichnis:

1. **Eigenständig.** Kein `requirements.txt`. Nur Standardbibliothek, sofern nicht zwingend nötig.
2. **`oc` zuerst, `kubectl` opt-in.** Das `--kubectl`-Flag aus `oom-extract.py` übernehmen, damit Skripte sowohl auf vanilla Kubernetes als auch auf OpenShift laufen.
3. **Argparse, keine Positional-Überraschungen.** Jedes Skript muss `--help` unterstützen.
4. **JSON-Modus.** Bei tabellarischer Ausgabe zusätzlich `--json` zum Pipen anbieten.
5. **Skript-Docstring auf eine Zeile beschränken.** Vollständige Doku gehört in dieses README unter einen neuen Abschnitt, eingetragen in der Übersichtstabelle oben.
6. **Ausführbar machen.** `chmod +x scripts/<name>.py` und Datei mit `#!/usr/bin/env python3` beginnen.
7. **Standardmäßig read-only.** Skripte, die Cluster-Zustand verändern, müssen ein explizites `--apply`-Flag verlangen und per Default einen Dry-Run ausführen.
