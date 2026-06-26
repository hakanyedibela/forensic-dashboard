# Cluster Usage & Right-Sizing — Architecture

How `fetch-cluster-usage.py`, `apply-recommendations.py`, and the CronJob work
together to measure real cluster usage, recommend right-sized CPU/memory/storage,
and archive the reports — safely (dry-run by default) and on a schedule.

> **Viewing this:** the diagrams below are [Mermaid](https://mermaid.js.org) and
> render automatically in **GitLab** and **GitHub** markdown and in VS Code
> (Mermaid preview). The doc is split into a one-screen **overview (for
> management)** and **detailed views (for admins)**.
>
> **Slide-ready images:** pre-rendered **PNG** (for PowerPoint/Keynote) and
> **SVG** (crisp at any zoom) of every diagram live in the `docs/diagrams/`
> folder — `01-overview`, `02-data-flow`, `03-cronjob-pipeline`,
> `04-apply-workflow`, `05-output-artifacts`. Regenerate
> them from the `.mmd` sources with the Mermaid CLI
> (`mmdc -i docs/diagrams/<name>.mmd -o docs/diagrams/<name>.png -b white -s 3`).

---

## 1. At a glance (overview)

What the system does, end to end — measure → recommend → review → archive.

```mermaid
flowchart LR
    subgraph CL["OpenShift cluster"]
        K[("Workloads and quotas<br/>CPU · memory · storage")]
        M[("Thanos / Prometheus<br/>real usage over time")]
    end

    K --> T
    M --> T
    T["fetch-cluster-usage.py<br/><b>measures usage vs. what's reserved</b>"]

    T --> R["Reports<br/>(CSV / Excel-friendly + text summary)"]
    T --> REC["Right-sizing recommendations<br/>(CPU / memory per workload)"]

    REC --> DR{"apply-recommendations.py<br/><b>dry-run safety check</b><br/>(changes nothing)"}
    DR --> REV["Admin reviews the report"]
    REV -->|approved| AP["Apply to cluster<br/>(optional, explicit)"]

    R --> GL[("GitLab repo<br/>full history / audit trail")]
    DR --> GL

    classDef src fill:#e8f0fe,stroke:#4285f4,color:#173;
    classDef tool fill:#fff4e5,stroke:#f5a623,color:#000;
    classDef out fill:#e6f4ea,stroke:#34a853,color:#000;
    classDef safe fill:#fde7e9,stroke:#d93025,color:#000;
    class K,M src; class T,REC tool; class R,GL,REV out; class DR,AP safe;
```

**Why it matters**
- **Capacity & cost** — see where CPU/memory/storage are over- or under-provisioned vs. real usage.
- **Right-sizing** — concrete per-workload recommendations, derived from measured peaks.
- **Safe by default** — recommendations are validated against the live cluster but **applied to nothing** unless a human explicitly approves.
- **Audit trail** — every run is committed to GitLab with date-stamped reports.

---

## 2. Data flow — what `fetch-cluster-usage.py` reads and writes (admin)

Stdlib-only Python. Runs **in-cluster** (REST API + ServiceAccount token) or
**locally** (`oc`/`kubectl`).

```mermaid
flowchart TB
    subgraph IN["Inputs"]
        direction TB
        A1["Kubernetes API<br/>pods · deployments/statefulsets/daemonsets<br/>ResourceQuota · PersistentVolumeClaims<br/>StorageClasses · namespaces"]
        A2["Thanos / Prometheus<br/>CPU rate · memory working-set<br/>peak & avg over --window"]
        A3["Live pod state<br/>lastState = OOMKilled"]
    end

    subgraph PROC["fetch-cluster-usage.py"]
        direction TB
        P1["Build tree<br/>namespace → workload → pod → container"]
        P2["Attach real usage<br/>(now / peak / avg)"]
        P3["Overlay ResourceQuota<br/>CPU/mem Hard+Used · storage per StorageClass"]
        P4["PVCs → capacity + StorageClass<br/>(+ description annotation)"]
        P5["compute_recommendation<br/>hot workloads → request/limit"]
        P6["namespace quota gate<br/>fits quota? else INCREASE_QUOTA"]
        P1 --> P2 --> P3 --> P4 --> P5 --> P6
    end

    subgraph OUT["Outputs (per run, also split by-stage)"]
        direction TB
        O1["resources.csv / -human.csv<br/>cluster→…→container + level=pvc rows"]
        O2["namespaces.csv / -human.csv<br/>CPU/mem + storage quota & PVC share %"]
        O3["recommendations.csv / -human.csv"]
        O4["recommendations-apply.yaml / .json<br/>+ apply/ folder (all · stage · namespace)"]
        O5["ooms.csv · report.json · summary.txt · LEGEND.md"]
    end

    A1 --> P1
    A2 --> P2
    A3 --> P1
    PROC --> O1 & O2 & O3 & O4 & O5

    classDef src fill:#e8f0fe,stroke:#4285f4,color:#000;
    classDef proc fill:#fff4e5,stroke:#f5a623,color:#000;
    classDef out fill:#e6f4ea,stroke:#34a853,color:#000;
    class A1,A2,A3 src; class P1,P2,P3,P4,P5,P6 proc; class O1,O2,O3,O4,O5 out;
```

**Recommendation rule (per hot workload, per resource):** a workload is "hot" when
its peak is unbounded or above the target utilisation (`--target-util`, default
80 %). Then `request = round_up(peak)` and `limit = round_up(peak ÷ target_util)`
(CPU → next 10m, memory → next Mi). Quiet workloads are omitted.

---

## 3. The CronJob pipeline (admin)

Every 5 days (06:00 Europe/Berlin) the job runs four steps in order, then pushes
to GitLab. **No image build, no runtime internet** — a static `oc` is copied
between containers; the scripts come from a ConfigMap.

```mermaid
flowchart TB
    CRON["CronJob: cluster-usage<br/>schedule 0 6 */5 * * · tz Europe/Berlin"]

    subgraph POD["Job pod (initContainers run in order, then push)"]
        direction TB
        S1["1 · get-oc<br/>image: origin-cli<br/>copy static oc → /tools"]
        S2["2 · fetch<br/>image: python:3.12-slim<br/>fetch-cluster-usage.py --in-cluster → /work"]
        S3["3 · apply<br/>image: python:3.12-slim<br/>apply-recommendations.py --oc (dry-run)<br/>→ /work/apply-report.txt"]
        S4["4 · push<br/>image: alpine/git<br/>commit /work → GitLab over HTTPS+PAT"]
        S1 --> S2 --> S3 --> S4
    end

    CRON --> POD

    VOLS["emptyDir volumes: /tools (oc) · /work (reports) · /gitwork"]
    SA["ServiceAccount: cluster-usage<br/>RBAC read: pods, quotas, PVCs,<br/>storageclasses, workloads, namespaces<br/>+ patch deploy/statefulset (server dry-run)"]
    SEC["Secret: gitlab-push (PAT)"]
    GL[("GitLab repo<br/>reports/&lt;YYYY-MM-DD&gt;/")]

    SA -. token .-> S2
    SA -. token .-> S3
    POD --- VOLS
    SEC -. PAT .-> S4
    S4 --> GL

    classDef cron fill:#ede7f6,stroke:#673ab7,color:#000;
    classDef step fill:#fff4e5,stroke:#f5a623,color:#000;
    classDef infra fill:#eceff1,stroke:#607d8b,color:#000;
    classDef out fill:#e6f4ea,stroke:#34a853,color:#000;
    class CRON cron; class S1,S2,S3,S4 step; class VOLS,SA,SEC infra; class GL out;
```

> The `apply` step is **server-side dry-run** (validates against the live cluster,
> mutates nothing) and is **non-fatal**, so the reports — including any validation
> findings — always reach GitLab. Switching it to real changes is a deliberate
> one-flag edit (`--execute`).

---

## 4. Apply workflow — `apply-recommendations.py` (admin)

Reads the machine-readable `recommendations-apply.json`, patches one workload at a
time via `oc`/`kubectl patch --type=strategic`. **Dry-run unless `--execute`.**

```mermaid
flowchart TB
    MAN["recommendations-apply.json<br/>(workload patches + quota-skipped list)"]
    MAN --> LOOP{"for each workload patch"}

    LOOP --> MODE{"--execute set?"}
    MODE -->|no default| DRY["oc patch --dry-run=server<br/>validate only · change nothing"]
    MODE -->|yes| EXE["oc patch<br/>apply for real"]

    DRY --> RES{"result"}
    EXE --> RES
    RES -->|OK| OK["record applied"]
    RES -->|NotFound| SK["skip (workload gone) — non-fatal"]
    RES -->|error| FA["record FAILED<br/>(non-zero exit under --execute)"]

    OK --> REP["apply-report.txt<br/>one line per applied recommendation"]
    SK --> REP
    FA --> REP

    NOTE["Quota-exceeding namespaces were already<br/>left out of the manifest (SKIPPED block,<br/>re-printed here) — raise quota first"]
    MAN -.-> NOTE

    classDef in fill:#e8f0fe,stroke:#4285f4,color:#000;
    classDef safe fill:#fde7e9,stroke:#d93025,color:#000;
    classDef act fill:#fff4e5,stroke:#f5a623,color:#000;
    classDef out fill:#e6f4ea,stroke:#34a853,color:#000;
    class MAN,NOTE in; class DRY safe; class EXE act; class REP,OK,SK,FA out;
```

**Apply scopes** — the `apply/` folder lets you choose how much to apply:
`apply/all.json` (everything), `apply/<stage>.json` (one stage), or
`apply/<stage>/<namespace>.json` (a single namespace).

---

## 5. Output artifacts (admin)

One run writes a self-contained bundle (repeated per stage under `by-stage/`):

```mermaid
flowchart LR
    RUN(["report run / one GitLab commit"])

    RUN --> CSV["CSV reports<br/>resources(-human) · namespaces(-human)<br/>recommendations(-human) · ooms"]
    RUN --> TXT["summary.txt<br/>human tables incl. STORAGE block"]
    RUN --> JSON["report.json<br/>full nested data"]
    RUN --> LEG["LEGEND.md<br/>column/row reference (DE)"]
    RUN --> APPLY["apply/<br/>all · &lt;stage&gt; · &lt;stage&gt;/&lt;namespace&gt;<br/>(.yaml review + .json for the applier)"]
    RUN --> AREP["apply-report.txt<br/>what the dry-run/execute did"]

    classDef run fill:#ede7f6,stroke:#673ab7,color:#000;
    classDef out fill:#e6f4ea,stroke:#34a853,color:#000;
    class RUN run; class CSV,TXT,JSON,LEG,APPLY,AREP out;
```

| Artifact | Audience | Contains |
|---|---|---|
| `summary.txt` | quick human read | per-namespace CPU/mem + **STORAGE** (quota, per-class PVC usage & share, PVC list) |
| `*-human.csv` | spreadsheet | same data, units inline (`200m`, `6.3Mi`, `61%`) — open in Excel |
| `*.csv` | machine | raw numbers for pipelines |
| `recommendations-apply.{yaml,json}` | review / apply | proposed right-sizing patches |
| `report.json` | integrations | full nested tree |

---

## 6. Glossary (for non-specialists)

| Term | Plain meaning |
|---|---|
| **request / limit** | the CPU/memory a workload *reserves* / its *ceiling* |
| **ResourceQuota** | a per-namespace cap on total CPU/memory/storage |
| **PVC** | a disk a workload mounts (PersistentVolumeClaim) |
| **StorageClass** | the type/tier of disk (e.g. `file-silver`) |
| **peak utilisation** | the highest real usage observed over the look-back window |
| **right-sizing** | adjusting requests/limits to match real usage (save waste, avoid OOM) |
| **dry-run** | a validated "what would happen" with **no actual change** |
| **OOMKilled** | a container killed for exceeding its memory limit |
