# fetch-cluster-state

Zwei zusammengehörige Skripte, die den aktuellen Zustand aller OpenShift-
Projekte mit Präfix `pid-` erfassen, die HPA-Bindings validieren und ein
wiederanwendbares „Desired State"-YAML erzeugen.

| Skript                                  | Aufgabe                                                              |
|-----------------------------------------|----------------------------------------------------------------------|
| `scripts/python/fetch-cluster-state.py` | Eigentlicher Worker. Läuft cluster-weit in einem einzigen Aufruf mit eingebauter Parallelisierung — oder gezielt für einzelne Projekte. |
| `scripts/fetch-cluster-state-loop.sh`   | Bash-Wrapper. Verwendet das gleiche Schleifenschema wie `lean-inspector-loop.sh` / `check-bind-resources.sh`: ermittelt `pid-*` Projekte, erkennt die Stage, ruft das Python-Skript einmal pro Namespace auf und fasst die CSVs zusammen. |

Nimm das Python-Skript für einen schnellen cluster-weiten Snapshot. Nimm
den Bash-Loop, wenn jeder Namespace isoliert verarbeitet werden soll
(eigene Output-Verzeichnisse, eigene `run.log`-Datei je Namespace,
robust gegenüber Einzel-Fehlern).

## Voraussetzungen

- `oc` (OpenShift CLI), am Ziel-Cluster eingeloggt
- Python 3.6.8+
- `pip install pyyaml` (Pflicht) und `pip install openpyxl` (optional, aktiviert die `.xlsx`-Datei)
- Für den Loop-Wrapper: `bash` 4+ (macOS liefert 3.2 mit; eine neuere Version per `brew install bash` installieren und im `PATH` vor `/bin/bash` stellen)

Leserechte auf die `pid-*` Projekte reichen aus — die Skripte schreiben
nichts in den Cluster.

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

## Ausgabe interpretieren

Wer noch nie einen solchen Report gelesen hat, geht in dieser Reihenfolge
vor. Jeder Schritt grenzt von „Was ist clusterweit kaputt?" auf „Was genau
ist mit diesem einen HPA kaputt?" ein.

### Schritt 1 — die beiden Top-Level-Übersichten lesen

Im Root des Reports liegen zwei Text-„Dashboards" mit unterschiedlichem Fokus:

| Datei                      | Fokus                                                                                  |
|----------------------------|----------------------------------------------------------------------------------------|
| `_master-overview.txt`     | **HPA-Gesundheit** je Namespace — `STATUS`, `HPAS`, `BAD` (fehlgeschlagene Validierungen). |
| `_resources-overview.txt`  | **Ressourcen-Footprint** je Namespace — Pods, CPU/Mem Req+Lim, PVCs, Storage, Quota %.  |

Zuerst `_master-overview.txt`. Danach `_resources-overview.txt` für den
Kapazitätskontext.

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

- **Schnell / weniger Dateien**: `fetch-cluster-state.py` direkt mit
  `--workers 4` (oder mehr).
- **Isolation pro Namespace, robust gegen Fehler, gleiches Muster wie
  die anderen `*-loop.sh`-Skripte**: `fetch-cluster-state-loop.sh`.

## Verhältnis zu den anderen Skripten

| Skript                        | Quelle           | Zweck                                              |
|-------------------------------|------------------|----------------------------------------------------|
| `check-bind-resources.sh`     | Live-Cluster     | Workload-Inventar: Pods, Deployments, STS, PVCs.   |
| `lean-inspector.sh`           | Lokale YAMLs     | Einmal-Inspector für Quotas / Limits / NetPol.     |
| `lean-inspector-loop.sh`      | Live-Cluster     | Lean-Inspector für jeden `pid-*` Namespace.        |
| `fetch-cluster-state.py`      | Live-Cluster     | Voller Snapshot + HPA-Validierung + Desired-State. |
| `fetch-cluster-state-loop.sh` | Live-Cluster     | Per-Namespace-Treiber rund um das Python-Skript.   |
