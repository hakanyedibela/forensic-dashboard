# fetch-cluster-state

Zwei zusammengehörige Skripte, die den aktuellen Zustand aller OpenShift-
Projekte mit Präfix `pid-` erfassen, die HPA-Bindings validieren und ein
wiederanwendbares „Desired State"-YAML erzeugen.

| Skript                         | Aufgabe                                                              |
|--------------------------------|----------------------------------------------------------------------|
| `fetch-cluster-state.py`       | Eigentlicher Worker. Läuft cluster-weit in einem einzigen Aufruf mit eingebauter Parallelisierung — oder gezielt für einzelne Projekte. |
| `fetch-cluster-state-loop.sh`  | Bash-Wrapper. Verwendet das gleiche Schleifenschema wie `lean-inspector-loop.sh` / `check-bind-resources.sh`: ermittelt `pid-*` Projekte, erkennt die Stage, ruft das Python-Skript einmal pro Namespace auf und fasst die CSVs zusammen. |

Nimm das Python-Skript für einen schnellen cluster-weiten Snapshot. Nimm
den Bash-Loop, wenn jeder Namespace isoliert verarbeitet werden soll
(eigene Output-Verzeichnisse, eigene `run.log`-Datei je Namespace,
robust gegenüber Einzel-Fehlern).

## Voraussetzungen

- `oc` (OpenShift CLI), am Ziel-Cluster eingeloggt
- Python 3.8+
- `pip install pyyaml`
- Für den Loop-Wrapper: `bash` 4+

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
python3 fetch-cluster-state.py [--output-dir DIR]
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
./fetch-cluster-state-loop.sh
```

Keine Argumente. Für jedes `pid-*` Projekt:

1. Stage erkennen.
2. `python3 fetch-cluster-state.py --project <ns>
   --output-dir state-loop-<ts>/by-stage/<stage>/<ns> --workers 1` aufrufen.
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
state-loop-20260513-101530/
├── by-stage/<stage>/<ns>/
│   ├── run.log
│   ├── _overview.json
│   ├── _hpa-validation.csv
│   ├── _dimensions.csv
│   └── by-stage/<stage>/<ns>/   (Output-Baum des Python-Skripts)
│       ├── snapshot.json
│       ├── hpa-bindings.json
│       └── desired/...
├── _hpa-validation.csv
├── _dimensions.csv
└── _master-overview.txt
```

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
