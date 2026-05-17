# fetch-cluster-state + fetch-cluster-oom

Zwei Pipelines, die den forensischen Zustand aller OpenShift-Projekte
mit Präfix `pid-` erfassen, plus ein kombinierter Wrapper, der beide
hintereinander ausführt:

- **fetch-cluster-state** — Cluster-State, HPA-Validierung, Ressourcen-
  Footprint, wiederanwendbare „Desired State"-YAML-Manifeste.
- **fetch-cluster-oom** — Root-Cause-Reports pro OOMKilled-Container
  inklusive Verdict (Pattern A/B/C/D/E) plus cluster-weiter Rollup.

| Skript                                  | Aufgabe                                                              |
|-----------------------------------------|----------------------------------------------------------------------|
| `scripts/python/fetch-cluster-state.py` | Der State-Worker. Läuft cluster-weit in einem Aufruf mit eingebauter Parallelisierung — oder gezielt für einzelne Projekte. |
| `scripts/fetch-cluster-state-loop.sh`   | Bash-Wrapper um den State-Worker. Ermittelt `pid-*` Projekte, erkennt die Stage, ruft das Python-Skript einmal pro Namespace auf und fasst die CSVs zusammen. |
| `scripts/python/fetch-cluster-oom.py`   | Der OOM-Worker. Baut pro OOMKilled-Container einen vollständigen Root-Cause-Report (Workload, Node, Nachbarn, Events, Autoscaling, Prometheus-Metriken, Pre-OOM-Logs, Verdict). |
| `scripts/fetch-cluster-oom-loop.sh`     | Bash-Wrapper um den OOM-Worker. Gleiche `pid-*`-Erkennung; schreibt Per-Namespace-JSON+Text nur dann, wenn tatsächlich OOMs gefunden werden. |
| `scripts/python/aggregate-resources.py` | Cluster-weiter Ressourcen-Rollup über die State-Loop-Ausgabe (Pods, CPU/Mem, PVCs, Quota %). |
| `scripts/python/aggregate-oom.py`       | Cluster-weiter OOM-Rollup (Pattern-Zählung, Findings-CSV) über die OOM-Loop-Ausgabe. |
| `scripts/fetch-all-loop.sh`             | Einmal-Wrapper: führt beide Loops in einen einzigen gemeinsamen Report-Ordner. **Nimm das, wenn du nicht weißt, wo du anfangen sollst.** |

Nimm die Python-Skripte für einen schnellen cluster-weiten Snapshot.
Nimm einen Bash-Loop, wenn jeder Namespace isoliert verarbeitet werden
soll (eigene Output-Verzeichnisse, eigene `run.log` je Namespace,
robust gegen Einzel-Fehler). Nimm `fetch-all-loop.sh`, wenn du einen
kombinierten Report über State und OOMs willst.

## Voraussetzungen

- `oc` (OpenShift CLI), am Ziel-Cluster eingeloggt
- Python 3.6.8+ (Standardbibliothek genügt für `fetch-cluster-oom.py` und die Aggregatoren; `fetch-cluster-state.py` braucht `pyyaml`)
- `pip install pyyaml` (Pflicht für `fetch-cluster-state.py`) und `pip install openpyxl` (optional, aktiviert die `.xlsx`-Datei)
- Für die Loop-Wrapper: `bash` 4+ (macOS liefert 3.2; per `brew install bash` eine neuere Version installieren und im `PATH` vor `/bin/bash` stellen)
- Optional für reichere OOM-Verdicts: ein erreichbares Prometheus (Default `http://localhost:9090`; per Port-Forward auf das Prometheus / Thanos im Cluster)

Leserechte auf die `pid-*` Projekte reichen aus — die Skripte schreiben
nichts in den Cluster. Cluster-weite Leserechte (`nodes`) helfen dem
OOM-Report, Noisy-Neighbor-Muster zu erkennen.

## Stage-Erkennung

Die Stage wird aus dem Namespace-Namen abgeleitet:

```
pid-<id>-<app>-<STAGE>-<num>-<suffix>
```

Beispiel: `pid-1000-akte-ref-100-prodda` → Stage `ref`.

Erkennungsreihenfolge:

1. Das 4. Segment (Index 3).
2. Jedes weitere Segment, das exakt einem bekannten Keyword entspricht.
3. Andernfalls `other`.

Bekannte Keywords: `ref`, `prod`, `test`, `phase`, `pnext`. Anpassbar über
die Konstante `STAGE_KEYWORDS` in beiden Skripten.

## fetch-cluster-state.py

```bash
python3 scripts/python/fetch-cluster-state.py [--output-dir DIR]
                                              [--stage STAGE [--stage STAGE ...]]
                                              [--project NS  [--project NS ...]]
                                              [--workers N]
```

Optionen:

- `--output-dir`: Basisverzeichnis (Default: `./state-<timestamp>`).
- `--stage`: nur Namespaces dieser Stage verarbeiten (mehrfach möglich).
- `--project`: nur die genannten Projekte verarbeiten (mehrfach möglich).
  Wenn gesetzt, wird die `oc get projects` Discovery übersprungen.
- `--workers`: Anzahl parallel verarbeiteter Namespaces (Default 4).

### Was pro Namespace erfasst wird

| Ressource                    | Erfasste Felder                                                |
|------------------------------|----------------------------------------------------------------|
| `Namespace`                  | Labels, Annotations, Environment-Label                         |
| `Deployment`                 | Replicas, Ready, Container-Anzahl, Images, summierte CPU/Mem Req+Lim |
| `StatefulSet`                | wie Deployment                                                 |
| `HorizontalPodAutoscaler`    | `scaleTargetRef`, Min/Max, aktuelle/gewünschte Replicas, Metrics, Status-Conditions, **Binding-Validierung** |
| `Service`                    | Type, ClusterIP, Ports                                         |
| `PersistentVolumeClaim`      | Phase, Speichergröße (GiB), StorageClass, AccessModes          |
| `ResourceQuota`              | `hard` + `used`                                                |
| `LimitRange`                 | komplette Limits-Spezifikation                                 |
| `NetworkPolicy`              | Namensliste                                                    |

### HPA-Binding-Validierung

Für jeden HPA werden geprüft:

- `scaleTargetRef.name` ist nicht leer.
- `scaleTargetRef.kind` ist `Deployment` oder `StatefulSet`.
- Das referenzierte Workload existiert tatsächlich im Namespace.
- `minReplicas` und `maxReplicas` sind gesetzt, und `min ≤ max`.
- Mindestens eine Metric ist konfiguriert.
- Bei einer `Resource`-Metric haben die Ziel-Container
  `resources.requests` gesetzt (sonst wird der HPA inaktiv).
- `ScalingActive=False` / `AbleToScale=False`-Bedingungen aus
  `.status.conditions` werden als Issues ausgewiesen.

Die Ergebnisse landen je Namespace in `hpa-bindings.json` und werden in
`_hpa-validation.csv` zusammengefasst.

### Desired-State-Ausgabe

Pro Namespace schreibt das Skript ein Verzeichnis `desired/` mit je einem
YAML pro Ressourcen-Art — bereinigt um serverseitig injizierte Felder
(`resourceVersion`, `uid`, `managedFields`, `creationTimestamp`,
`last-applied-configuration`, `deployment.kubernetes.io/revision`, ...).

Die Dateien sind numerisch präfixiert, damit `oc apply -f desired/` sie
in der richtigen Reihenfolge anwendet:

```
00-namespace.yaml
10-resourcequotas.yaml
20-limitranges.yaml
30-networkpolicies.yaml
40-deployments.yaml
41-statefulsets.yaml
50-services.yaml
60-pvcs.yaml
70-hpas.yaml
```

Idee: `desired/` in Git einchecken, bearbeiten, dann mit
`oc diff -f desired/` und `oc apply -f desired/` den Zielzustand
herstellen.

### Ausgabestruktur

```
state-20260513-101530/
├── by-stage/
│   ├── ref/
│   │   └── pid-1000-akte-ref-100-prodda/
│   │       ├── snapshot.json
│   │       ├── hpa-bindings.json
│   │       └── desired/
│   │           ├── 00-namespace.yaml
│   │           └── ...
│   └── prod/
├── _overview.json
├── _hpa-validation.csv
└── _dimensions.csv
```

## fetch-cluster-state-loop.sh

Umschließt das Python-Skript mit derselben `pid-*`-Schleife wie
`check-bind-resources.sh` und `lean-inspector-loop.sh`.

```bash
./scripts/fetch-cluster-state-loop.sh
```

Keine Argumente. Die Ausgaben landen unter `./reports/state-loop-<timestamp>/`
relativ zum aktuellen Arbeitsverzeichnis — am besten aus dem Repo-Root
aufrufen, dann sammelt sich alles in `<repo>/reports/`.

Für jedes `pid-*` Projekt:

1. Stage erkennen.
2. `python3 scripts/python/fetch-cluster-state.py --project <ns>
   --output-dir reports/state-loop-<ts>/by-stage/<stage>/<ns> --workers 1`
   aufrufen.
3. stdout/stderr nach `run.log` im Namespace-Ordner umleiten.
4. Die je Namespace erzeugten `_hpa-validation.csv` und `_dimensions.csv`
   in kombinierte Dateien im Root-Verzeichnis zusammenführen.
5. `_master-overview.txt` mit Status je Namespace, HPA-Issue-Zählung und
   Per-Stage-Summen schreiben.

Schlägt ein einzelner Namespace fehl, läuft die Schleife weiter und
markiert ihn in der Master-Übersicht als `FAIL`. Der Fehler ist
zusätzlich in der `run.log` dieses Namespaces sichtbar.

### Ausgabestruktur

```
reports/state-loop-20260513-101530/
├── by-stage/<stage>/<ns>/
│   ├── run.log
│   ├── _overview.json
│   ├── _hpa-validation.csv
│   ├── _dimensions.csv
│   ├── _cluster-state.xlsx                (falls openpyxl installiert)
│   └── by-stage/<stage>/<ns>/             (Output-Baum des Python-Skripts)
│       ├── snapshot.json
│       ├── hpa-bindings.json
│       └── desired/...
├── _hpa-validation.csv                    (aggregiert über alle Namespaces)
├── _dimensions.csv                        (aggregiert über alle Namespaces)
├── _master-overview.txt                   (HPA-zentriert: Status, BAD-Anzahl)
├── _resources-overview.txt                (Ressourcen: Pods, CPU/Mem, PVCs, Quota%)
└── _resources-overview.csv                (dasselbe, Rohwerte, zum Pivotieren)
```

> **Hinweis** — der Per-Namespace-Baum erscheint zweimal unter
> `by-stage/<stage>/<ns>/` (einmal vom Loop, einmal vom Python-Skript
> angelegt). Die aggregierten CSVs auf der obersten Ebene sind davon nicht
> betroffen; nur die Per-Namespace-Artefakte (`snapshot.json`,
> `hpa-bindings.json`, `desired/`) liegen eine Ebene tiefer als erwartet.

## fetch-cluster-oom.py

```bash
python3 scripts/python/fetch-cluster-oom.py [-n NS | -A] [--pod NAME]
                                            [--summary | --json]
                                            [--logs [--grep REGEX]]
                                            [--prometheus-url URL]
                                            [--prometheus-port N]
                                            [--token TOKEN]
                                            [--insecure]
                                            [--no-prometheus]
                                            [--diagnose]
                                            [--kubectl]
```

Für jeden Container, dessen `lastState.terminated.reason == OOMKilled`
ist, baut das Skript einen vollständigen Root-Cause-Report und
schließt mit einem Verdict (Pattern A/B/C/D/E oder `?`) ab.

### Was pro OOMKilled-Container erfasst wird

| Quelle                  | Was erfasst wird                                                                  |
|-------------------------|-----------------------------------------------------------------------------------|
| Pod- / Container-Spec   | Image, Requests/Limits, QoS-Klasse, Probes, Args, Exit-Code, Restart-Count        |
| Workload-Auflösung      | folgt `ownerReferences` einmal: ReplicaSet → Deployment, etc.                     |
| Node                    | Capacity, Allocatable, Pressure-Conditions                                        |
| Nachbar-OOMs            | andere OOMs auf demselben Node innerhalb ±1h (Beweis für Noisy-Neighbor-Pattern)  |
| Events                  | Pod-Events der letzten Stunde                                                     |
| Storage                 | PVCs, die der Pod mountet (Phase, Capacity, StorageClass, AccessModes)            |
| Autoscaling             | passender HPA + VPA (falls vorhanden), mit current/desired Replicas + Metriken    |
| Network                 | Services, die diesen Pod selektieren; NetworkPolicies, deren podSelector matched  |
| Namespace-Constraints   | LimitRanges, ResourceQuotas mit used/hard                                         |
| Prometheus (optional)   | Working-Set, Peaks (5m/1h), Slope (deriv 1h), Memory-Limit, CPU rate/throttle, rx/tx, fs reads/writes |
| Pre-OOM-Logs (optional) | `oc logs --previous`, gefiltert per Regex (Default: `error|oom|killed|out ?of ?memory|fatal|exception`) |

### Verdicts — die A/B/C/D/E-Patterns

Das Verdict ist das erste passende Pattern in Prioritätsreihenfolge;
alles andere landet als `?` für manuelle Analyse.

| Pattern | Auslöser                                                                                 | Typische Ursache                                                            |
|---------|------------------------------------------------------------------------------------------|------------------------------------------------------------------------------|
| `E`     | lifetime < 60s **und** restart_count ≥ 2                                                 | App kann sich nicht innerhalb des Memory-Limits initialisieren (JVM `-Xmx`, großes ML-Modell, großer Cache-Warmup) |
| `D`     | ≥ 2 weitere Pods auf demselben Node innerhalb ±1h OOMed                                  | Node überbucht; fehlendes Limit bei einem Nachbarn oder Kernel-OOM           |
| `A`     | Memory-Slope (1h) ≥ ~17 KiB/s **und** peak/limit ≥ 85 %                                  | Stetiges Memory-Wachstum → Leak. Limit ist die Decke, nicht die Ursache       |
| `B`     | Netzwerk-Rx (1m) ≥ 3 × Baseline-Rx (30m-Mittel der 5m-Rate)                              | Spike / großer Request — Body im Speicher gebuffert                          |
| `C`     | peak/limit ≥ 95 % **und** Slope flach                                                    | Normaler Peak ≥ Memory-Limit — Workload ist unter-provisioniert               |
| `?`     | nichts davon mit Sicherheit                                                              | Indeterminate — manuelle Analyse                                              |

Jedes Verdict kommt mit einer `evidence`-Liste (was beobachtet wurde)
und einer `fixes`-Liste (konkrete `oc`-Befehle für die weitere Analyse).

### Ausgabe-Modi

| Flag         | Ausgabe                                                                                  |
|--------------|------------------------------------------------------------------------------------------|
| (keiner)     | Voller Text-Report pro OOM (Sektionen: CONTEXT, CONFIGURATION, MEMORY, CPU, NETWORK, STORAGE, NODE, NEIGHBORS, AUTOSCALING, SERVICES & NETPOL, NAMESPACE CONSTRAINTS, EVENTS, VERDICT) |
| `--summary`  | Eine Zeile pro OOM (`NAMESPACE  POD  CONTAINER  PATTERN  OOM AGE`). Schnelle Triage.     |
| `--json`     | Strukturiertes Array (ein Eintrag pro OOM). Das verwendet der Loop-Wrapper für `aggregate-oom.py`. |
| `--diagnose` | Probe-Modus: zeigt, welche Prometheus-Scrape-Sources gesund / fehlen, dann Ende.         |

### Voraussetzungen für den Prometheus-Pfad

Resource-Metric-Verdicts (A, B, C) feuern nur, wenn das Skript
Prometheus erreichen kann. Ohne Prometheus ist der Verdict-Pfad auf
D (Nachbarn) und E (Lifetime) beschränkt — immer noch nützlich, aber
blind für die Memory-Form. `--diagnose` prüft, welche Metric-Families
verfügbar sind, und gibt Installations-Hinweise aus, falls etwas fehlt.

## fetch-cluster-oom-loop.sh

Wrappt den OOM-Worker genauso wie `fetch-cluster-state-loop.sh` den
State-Worker.

```bash
./scripts/fetch-cluster-oom-loop.sh [--report-dir DIR]
```

Default-Output ist `./reports/state-loop-<timestamp>/` (gleicher
Verzeichnisname wie beim State-Loop, damit beide Reports per
`--report-dir` oder via `fetch-all-loop.sh` denselben Ordner teilen
können). Für jedes `pid-*` Projekt:

1. Stage erkennen (gleiche Regel wie beim State-Loop).
2. `python3 scripts/python/fetch-cluster-oom.py -n <ns> --json` in eine
   Temp-Datei aufrufen.
3. Hat der Namespace **null** OOMKilled-Container, wird die Temp-Datei
   verworfen und **kein Per-Namespace-Verzeichnis angelegt**.
4. Bei OOMs wird `by-stage/<stage>/<ns>/` materialisiert mit
   `report.json`, `report.txt`, `oom-run.log`.
5. Nach der Schleife läuft `aggregate-oom.py` und schreibt
   `_oom-overview.txt` + `_oom-findings.csv` + `_oom-status.txt`.
6. Hat der ganze Cluster null OOMs, schreibt der Loop nichts und (wenn
   er das Report-Verzeichnis selbst angelegt hat) entfernt es am Ende.

### Umgebungsvariablen (Prometheus opt-in)

Der OOM-Loop verwendet per Default `--no-prometheus`, damit ein Lauf
schnell und vorhersagbar ist. Prometheus per Env-Variablen aktivieren:

| Variable                | Wirkung                                               |
|-------------------------|-------------------------------------------------------|
| `OOM_PROMETHEUS_URL`    | wird an `--prometheus-url` durchgereicht              |
| `OOM_PROMETHEUS_PORT`   | wird an `--prometheus-port` durchgereicht             |
| `OOM_TOKEN`             | wird an `--token` durchgereicht                       |
| `OOM_INSECURE=1`        | wird an `--insecure` durchgereicht                    |

Sind weder URL noch PORT gesetzt, hängt der Loop `--no-prometheus` an
und das Verdict ist auf Patterns D / E (oder `?`) beschränkt.

### Ausgabestruktur (nur wenn OOMs gefunden werden)

```
reports/state-loop-20260513-101530/
├── by-stage/<stage>/<ns>/                # nur Namespaces mit OOMs
│   ├── oom-run.log
│   ├── report.json                       # vollständiges, strukturiertes JSON
│   └── report.txt                        # menschenlesbarer Per-Namespace-Report
├── _oom-overview.txt                     # Per-Namespace + Per-Stage-Rollup
├── _oom-findings.csv                     # eine Zeile pro OOMKilled-Container
└── _oom-status.txt                       # bash-seitige Fallback-Übersicht
```

## fetch-all-loop.sh — kombinierter Wrapper

Wenn du **einen Report über State UND OOMs** willst, dieses Skript
verwenden und die einzelnen Loops überspringen:

```bash
./scripts/fetch-all-loop.sh
```

Es legt ein einziges `./reports/state-loop-<timestamp>/` an und ruft
dann `fetch-cluster-state-loop.sh` und `fetch-cluster-oom-loop.sh`
nacheinander auf — beide mit `--report-dir` auf dieses Verzeichnis.
Dateinamen sind so präfixiert, dass die Artefakte beider Skripte
nebeneinander leben können (`_master-*`, `_resources-*`, `_hpa-*`,
`_dimensions.csv` vom State; `_oom-*` vom OOM; per-Namespace `run.log`
vs `oom-run.log`).

Kombiniertes Layout (wenn OOMs gefunden wurden):

```
reports/state-loop-20260513-101530/
├── by-stage/<stage>/<ns>/
│   ├── run.log                           (state)
│   ├── oom-run.log                       (oom — nur bei OOMs im ns)
│   ├── _overview.json                    (state, pro ns)
│   ├── _hpa-validation.csv               (state, pro ns)
│   ├── _dimensions.csv                   (state, pro ns)
│   ├── _cluster-state.xlsx               (state, pro ns)
│   ├── report.json                       (oom, nur bei OOMs im ns)
│   ├── report.txt                        (oom, nur bei OOMs im ns)
│   └── by-stage/<stage>/<ns>/            (state, doppelt verschachtelt)
│       ├── snapshot.json
│       ├── hpa-bindings.json
│       └── desired/...
├── _master-overview.txt                  (state)
├── _resources-overview.txt + .csv        (state)
├── _hpa-validation.csv                   (state, aggregiert)
├── _dimensions.csv                       (state, aggregiert)
├── _oom-overview.txt                     (oom, nur bei OOMs)
├── _oom-findings.csv                     (oom, nur bei OOMs)
└── _oom-status.txt                       (oom, nur bei OOMs)
```

Das Abschluss-Echo druckt nur Pfade zu Dateien, die wirklich existieren
— Läufe ohne OOMs erzeugen eine saubere Zusammenfassung ohne tote Links.

## Ausgabe interpretieren

Wer noch nie einen solchen Report gelesen hat, geht in dieser Reihenfolge
vor. Jeder Schritt grenzt von „Was ist clusterweit kaputt?" auf „Was genau
ist mit diesem einen HPA kaputt?" ein.

### Schritt 1 — die Top-Level-Übersichten lesen

Im Root des Reports liegen bis zu drei Text-„Dashboards" mit
unterschiedlichem Fokus:

| Datei                      | Fokus                                                                                  | Vorhanden, wenn               |
|----------------------------|----------------------------------------------------------------------------------------|--------------------------------|
| `_master-overview.txt`     | **HPA-Gesundheit** je Namespace — `STATUS`, `HPAS`, `BAD` (fehlgeschlagene Validierungen). | State-Loop lief                |
| `_resources-overview.txt`  | **Ressourcen-Footprint** je Namespace — Pods, CPU/Mem Req+Lim, PVCs, Storage, Quota %.  | State-Loop lief                |
| `_oom-overview.txt`        | **OOM-Aktivität** je Namespace — `STATUS`, `OOMS`, `PATTERNS` (z. B. `A x2, C x1`).     | OOM-Loop fand ≥ 1 OOM          |

Zuerst `_master-overview.txt`. Danach `_resources-overview.txt` für den
Kapazitätskontext. Danach `_oom-overview.txt`, falls vorhanden — sein
Fehlen sagt bereits aus, dass der Cluster zum Zeitpunkt des Laufs null
OOMKills hatte.

#### `_master-overview.txt`

Eine Zeile je Namespace plus Stage-Summen.

```
STAGE    NAMESPACE                          STATUS  HPAS  BAD
phase    pid-004-batch-phase-01-blue        ok       1     1
prod     pid-003-web-prod-01-blue           ok       1     1
ref      pid-001-shop-ref-01-blue           ok       1     0
```

| Spalte      | Bedeutung                                                              |
|-------------|------------------------------------------------------------------------|
| `STATUS`    | Lief der Python-Aufruf durch? `ok` oder `FAIL` (siehe `run.log`).      |
| `HPAS`      | Anzahl HPAs, die im Namespace existieren.                              |
| `BAD`       | Anzahl HPAs, die die Validierung nicht bestanden haben (issues > 0).   |

**Zuerst hier durchsehen.** Wenn alles `STATUS=ok` und `BAD=0` ist, kann
man aufhören. Andernfalls → Schritt 2.

#### `_resources-overview.txt`

Gleicher Aufbau, andere Spalten:

```
STAGE    NAMESPACE                              PODS    WL  CPU_REQ  CPU_LIM  MEM_REQ  MEM_LIM PVCS  STOR QUOTAS LR SVCS NP QMAX%
test     pid-002-api-test-01-blue                  3   1/1     250m     700m    160Mi    320Mi    0     0      1  1    2  0   30%
```

| Spalte                | Bedeutung                                                                              |
|-----------------------|----------------------------------------------------------------------------------------|
| `PODS`                | Summe von `replicas` über alle Deployments und StatefulSets.                           |
| `WL`                  | `<deployments>/<statefulsets>`-Anzahl.                                                 |
| `CPU_REQ` / `CPU_LIM` | Summe `replicas × container requests/limits` über alle Workloads.                      |
| `MEM_REQ` / `MEM_LIM` | Dasselbe für Memory.                                                                   |
| `PVCS`                | Anzahl PersistentVolumeClaims.                                                         |
| `STOR`                | Summe der PVC-Storage in GiB.                                                          |
| `QUOTAS` / `LR`       | Anzahl ResourceQuotas / LimitRanges im Namespace.                                      |
| `SVCS` / `NP`         | Anzahl Services / NetworkPolicies.                                                     |
| `QMAX%`               | Höchstes `used / hard`-Verhältnis über **alle** Quota-Dimensionen. `-` ohne Quota.     |

Nützlich, um zu erkennen:
- Workloads **ohne Requests** (`CPU_REQ=0m, MEM_REQ=0Mi` bei aktiven Pods).
- Namespaces nahe an der Quota-Grenze (`QMAX%` ≥ 80 %).
- Ausreißer im Ressourcen-Footprint zwischen Stages (z. B. `prod` < `test`).

`_resources-overview.csv` enthält dieselben Daten als Rohwerte
(Millicores, MiB, GiB) für die Auswertung im Spreadsheet.

### Schritt 2 — aggregierte `_hpa-validation.csv` öffnen

(Diesen Schritt überspringen, wenn `_master-overview.txt` für jeden
Namespace `BAD=0` zeigt.)


Eine Zeile je HPA über den gesamten Cluster. Nach `ok` filtern oder
sortieren:

| Feld                  | Bedeutung                                                                                |
|-----------------------|------------------------------------------------------------------------------------------|
| `ok`                  | `True` = keine Probleme gefunden, `False` = mindestens ein Problem.                      |
| `targetFound`         | Verweist `scaleTargetRef` auf eine existierende Deployment/StatefulSet?                  |
| `targetHasRequests`   | Haben *alle* Container des Targets `resources.requests` gesetzt?                         |
| `targetSpecReplicas`  | Aktuell konfigurierte Replicas des Targets.                                              |
| `currentReplicas`     | Live `.status.currentReplicas` des HPA.                                                  |
| `desiredReplicas`     | Live `.status.desiredReplicas` des HPA.                                                  |
| `metricsCount`        | Anzahl Einträge in `.spec.metrics`.                                                      |
| `issues`              | **Semikolon-getrennte** Liste aller gefundenen Probleme. Das ist die Begründung.          |

Zeilen mit `ok=False` sind die zu bearbeitenden Fälle. Die Spalte
`issues` ist das Urteil.

### Schritt 3 — `by-stage/<stage>/<ns>/.../hpa-bindings.json` für Kontext

Für einen konkreten fehlerhaften HPA enthält diese Datei dieselben
Informationen strukturiert, zusätzlich die komplette Liste der
`.status.conditions` von Kubernetes. Nützlich, wenn die CSV-Meldung vage
ist (z. B. „condition ScalingActive=False") und man die zugehörige
`reason` / `message` von Kubernetes braucht.

### Schritt 4 — erst dann in die Per-Namespace-Artefakte schauen

| Datei                               | Was sie liefert                                                                          |
|-------------------------------------|------------------------------------------------------------------------------------------|
| `snapshot.json`                     | Normalisierter Ist-Zustand. Keine Urteile — nur Fakten (Replicas, Requests, Images).     |
| `desired/*.yaml`                    | Wiederanwendbare Manifeste ohne Runtime-Felder. Gegen Git diffen oder neu anwenden.       |
| `_dimensions.csv`                   | Per-Workload CPU/Memory Requests & Limits. Für Capacity- / Sizing-Reviews.                |
| `_overview.json`                    | Dieselben Kennzahlen wie `_master-overview.txt`, strukturiert.                            |
| `_cluster-state.xlsx`               | Excel-Workbook mit Bedingungs-Formatierung (rot/amber/grün). Bequemste visuelle Ansicht. |
| `run.log`                           | stdout/stderr des Python-Aufrufs für diesen Namespace.                                    |

### Was die Farben in `_cluster-state.xlsx` bedeuten

| Farbe | Bedeutung                                                                  |
|-------|----------------------------------------------------------------------------|
| Rot   | HPA hat mindestens ein Problem, PVC nicht gebunden oder Quota-Nutzung ≥ 95 %. |
| Amber | Quota-Nutzung 80–95 % oder Workload ohne gesetzte `resources.requests`.    |
| Grün  | HPA hat die Validierung sauber bestanden.                                  |

### Häufige HPA-`issues`-Meldungen

| Meldung                                                                            | Ursache                                                                                              | Behebung                                                                       |
|------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------|
| `target Deployment/<name> not found in namespace`                                  | `scaleTargetRef` zeigt auf eine Deployment/StatefulSet, die nicht existiert.                         | `scaleTargetRef.name` korrigieren oder fehlendes Workload anlegen.             |
| `HPA uses Resource metric but target containers have no resources.requests`        | Eine `Resource`-Metric (z. B. CPU) braucht gesetzte `resources.requests` auf den Ziel-Containern.    | `resources.requests.cpu` / `memory` an den Containern des Deployments setzen.   |
| `no metrics configured`                                                            | `.spec.metrics` ist leer.                                                                            | Mindestens einen Metric-Eintrag setzen.                                        |
| `minReplicas missing` / `maxReplicas missing` / `minReplicas (N) > maxReplicas (M)`| Spec-Konfigurationsfehler.                                                                           | Beide setzen, mit `min ≤ max`.                                                 |
| `unsupported scaleTargetRef.kind='<kind>'`                                         | HPA-Ziel ist weder `Deployment` noch `StatefulSet`.                                                  | HPA auf einen unterstützten Kind umstellen.                                    |
| `condition ScalingActive=False (FailedGetResourceMetric)`                          | Clusterseitig: der HPA-Controller kann keine Metriken abrufen. Meist fehlt `metrics-server`.          | `metrics-server` installieren. Die HPA-Spec selbst ist in Ordnung.              |
| `condition AbleToScale=False (FailedGetScale)`                                     | Clusterseitig: der HPA kann die aktuelle Skala nicht ermitteln (oft, weil das Target fehlt).         | Tritt meist zusammen mit `target ... not found` auf — zuerst das beheben.       |

### OOM-Übersicht lesen (`_oom-overview.txt`)

Gleicher Aufbau wie `_master-overview.txt`, andere Spalten:

```
STAGE    NAMESPACE                          STATUS  OOMS  PATTERNS
prod     pid-003-web-prod-01-blue           ok       2    A x2
phase    pid-004-batch-phase-01-blue        ok       1    E x1
```

| Spalte     | Bedeutung                                                                              |
|------------|----------------------------------------------------------------------------------------|
| `STATUS`   | Lief `fetch-cluster-oom.py` für den Namespace durch? `ok` / `FAIL` (siehe `oom-run.log`). |
| `OOMS`     | Anzahl OOMKilled-Container im Namespace.                                                  |
| `PATTERNS` | Kompakte Zählung der Verdict-Patterns über diese OOMs (z. B. `A x2, C x1`).               |

Pattern-Legende:

| Code | Bedeutung                                                                |
|------|--------------------------------------------------------------------------|
| `A`  | MEMORY LEAK — Memory wuchs stetig, Limit wurde erreicht                   |
| `B`  | SPIKE / LARGE REQUEST — Traffic-Burst vor dem OOM                         |
| `C`  | UNDER-PROVISIONED LIMIT — normaler Peak ≥ Limit                           |
| `D`  | NODE PRESSURE / NOISY NEIGHBOR — andere Pods OOMed auf demselben Node     |
| `E`  | STARTUP OVERRUN — App kann sich nicht im Limit initialisieren             |
| `?`  | INDETERMINATE — manuelle Analyse nötig (oft: Prometheus nicht erreichbar) |

Danach `_oom-findings.csv` öffnen für eine Zeile pro OOMKilled-Container
mit pivot-tauglichen Spalten: `stage, namespace, pod, container, node,
workload, oom_at, age, lifetime_s, restart_count, exit_code,
pattern_short, pattern, evidence`. `pattern_short` ist eines von
`A/B/C/D/E/?`, fertig zum Filtern.

Für die Detailanalyse eines konkreten OOMs den Per-Namespace
`report.txt` (Volltext) oder `report.json` (strukturiert) unter
`by-stage/<stage>/<ns>/` öffnen.

> **Wichtig** — hat ein Namespace null OOMs, schreibt der Loop für ihn
> **nichts**: kein Per-Namespace-Verzeichnis, keine `report.json`,
> keine `report.txt`. Ein fehlender Ordner unter `by-stage/` bedeutet,
> dass dieser Namespace gesund war — nicht dass der Loop fehlgeschlagen
> ist.

### Konfigurationsfehler vs. clusterseitige Bedingung

Der Validator unterscheidet nicht zwischen *Manifest ist falsch* und
*Cluster kann es nicht auswerten*. Beides landet als `ok=False`. Faustregel:

- Meldungen, die mit **`condition ...=False (...)`** beginnen, werden vom
  Kubernetes-HPA-Controller gemeldet und deuten meist auf ein
  clusterseitiges Problem hin (fehlender `metrics-server`, RBAC, Netzwerk)
  und nicht auf einen Manifest-Defekt.
- Alle anderen Meldungen sind **statische Konfigurationsfehler**, die
  dieses Skript anhand der Manifeste allein erkennt — sie wären auf jedem
  Cluster falsch.

Beispiel: auf einem lokalen CRC-Cluster ohne `metrics-server` zeigt
*jeder* HPA mit Resource-Metric `ScalingActive=False`, obwohl die Spec
korrekt ist. Nach `metrics-server`-Installation kippt das auf `ok=True`.


## Welches Skript wann

- **Ich weiß nicht, wo ich anfangen soll — gib mir einen kombinierten
  Report**: `./scripts/fetch-all-loop.sh`.
- **Schnell / wenige Dateien / ein cluster-weiter Snapshot**:
  `fetch-cluster-state.py` direkt mit `--workers 4` (oder mehr).
- **Isolation pro Namespace, robust gegen Fehler**:
  `fetch-cluster-state-loop.sh`.
- **Einen konkreten OOM tief untersuchen**: `fetch-cluster-oom.py`
  direkt mit `-n <ns> --pod <name> --logs --prometheus-url <url>`.
- **Cluster-weiter OOM-Sweep**: `fetch-cluster-oom-loop.sh` (oder der
  kombinierte `fetch-all-loop.sh`).

## Verhältnis zu den anderen Skripten

| Skript                          | Quelle            | Zweck                                                  |
|---------------------------------|-------------------|--------------------------------------------------------|
| `check-bind-resources.sh`       | Live-Cluster      | Workload-Inventar: Pods, Deployments, STS, PVCs.       |
| `lean-inspector.sh`             | Lokale YAMLs      | Einmal-Inspector für Quotas / Limits / NetPol.         |
| `lean-inspector-loop.sh`        | Live-Cluster      | Lean-Inspector für jeden `pid-*` Namespace.            |
| `fetch-cluster-state.py`        | Live-Cluster      | Voller Snapshot + HPA-Validierung + Desired-State.     |
| `fetch-cluster-state-loop.sh`   | Live-Cluster      | Per-Namespace-Treiber rund um den State-Worker.        |
| `fetch-cluster-oom.py`          | Live-Cluster (+Prom) | Per-OOM-Root-Cause-Report mit A/B/C/D/E-Verdict.    |
| `fetch-cluster-oom-loop.sh`     | Live-Cluster (+Prom) | Per-Namespace-Treiber rund um den OOM-Worker.       |
| `aggregate-resources.py`        | State-Loop-Output | Ressourcen-Rollup `_resources-overview.{txt,csv}`.     |
| `aggregate-oom.py`              | OOM-Loop-Output   | OOM-Rollup `_oom-overview.txt` + `_oom-findings.csv`.  |
| `fetch-all-loop.sh`             | Live-Cluster (+Prom) | Kombinierter Wrapper: State-Loop + OOM-Loop in einem Ordner. |
