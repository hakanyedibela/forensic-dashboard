# OOM simulation samples

Synthetic workloads that reproduce each OOM pattern documented in the top-level README. Use them to validate the Thanos dashboard and alert rules.

## Layout

```
samples/
├── namespace.yaml              # oom-test namespace
├── simulator/
│   ├── app.py                  # multi-mode Python OOM simulator (canonical source)
│   ├── Dockerfile              # optional — only if you want to build/push an image
│   └── configmap.yaml          # app.py wrapped as ConfigMap (zero-build path)
└── patterns/
    ├── pattern-a-leak.yaml     # steady memory leak → OOM every ~50s
    ├── pattern-b-spike.yaml    # HTTP-driven spike (needs loadgen)
    ├── pattern-c-baseline.yaml # high baseline, no leak, limit too small
    ├── pattern-e-startup.yaml  # startup allocation > limit, dies in seconds
    └── loadgen.yaml            # curl loop to trigger pattern-b
```

No custom image required — deployments mount `app.py` from a ConfigMap into `python:3.12-alpine`.

## Deploy (zero build)

```bash
cd ~/rke2-observability/samples
kubectl apply -f namespace.yaml
kubectl apply -f simulator/configmap.yaml
kubectl apply -f patterns/                 # all four patterns + loadgen
```

Or run one pattern at a time:

```bash
kubectl apply -f patterns/pattern-a-leak.yaml
```

Watch it go:

```bash
kubectl -n oom-test get pods -w
kubectl -n oom-test describe pod -l app=leak-app | grep -A3 'Last State'
kubectl -n oom-test logs -l app=leak-app --tail=30
```

## What each pattern produces

| Pattern | Deployment | Expected dashboard signals | Typical time-to-OOM |
|---|---|---|---|
| **A — Leak** | `leak-app` | Panel 6 positive slope; Panel 5 climbs 0→100%; sawtooth on Panel 2 | ~50 s |
| **B — Spike** | `spike-app` + `spike-loadgen` | Panel 6 flat with bumps; P99 ≫ P95; OOMs aligned with loadgen timestamps | ~30 s after first spike |
| **C — Baseline** | `baseline-app` | Panel 5 flat at ~94%; Panel 6 zero; P95 ≈ P99; occasional OOM | hours |
| **E — Startup** | `startup-app` | Panel 9 < 10 s; restarts climb fast; CrashLoopBackOff | seconds |

## Trigger manually

Spike app exposes endpoints on port 8080:

```bash
kubectl -n oom-test port-forward svc/spike-app 8080:8080

curl http://localhost:8080/spike     # 10s transient 180 MiB allocation (may OOM)
curl http://localhost:8080/leak      # permanently leak 180 MiB
curl http://localhost:8080/free      # release everything
curl http://localhost:8080/stats     # show current held MiB
```

## Tune parameters

Every simulator knob is an env var. Patch a deployment without re-applying:

```bash
# bump leak rate to 20 MiB/s
kubectl -n oom-test set env deploy/leak-app LEAK_MB_PER_SEC=20

# make spike larger so it OOMs every time
kubectl -n oom-test set env deploy/spike-app SPIKE_MB=300

# relax a limit to see Panel 5 stabilize below 100%
kubectl -n oom-test set resources deploy/baseline-app \
  --limits=memory=512Mi --requests=memory=200Mi
```

Env var reference:

| Var | Default | Meaning |
|---|---|---|
| `MODE` | `leak` | `leak \| spike \| baseline \| startup \| idle` |
| `LEAK_MB_PER_SEC` | `5` | MiB/s growth in leak mode |
| `BASELINE_MB` | `100` | MiB held in baseline mode |
| `STARTUP_MB` | `500` | MiB allocated at boot in startup mode |
| `SPIKE_MB` | `50` | MiB per `/spike` or `/leak` request |
| `PORT` | `8080` | HTTP port for spike mode |

## Verify against the dashboard

1. Open Grafana → **OOMKilled — Thanos Deep Dive**
2. Set `namespace = oom-test`
3. For each running pattern, confirm the signals in the table above
4. Open **Explore → Loki** at an OOM timestamp:
   ```
   {namespace="oom-test"} |~ "(?i)killed|memory|oom"
   ```
5. Check Alertmanager fires:
   - `ContainerOOMKilled` — should appear within 1 min of first kill
   - `ContainerNearMemoryLimit` — Pattern C after 15 min
   - `MemoryLeakSuspect` — Pattern A after 2 h
   - `FrequentOOMKills` — Pattern A or B within 1 h

## Cleanup

```bash
kubectl delete namespace oom-test
```

That removes every pattern deployment, the simulator ConfigMap, loadgen, and their metrics series (after Prometheus retention expires).

## Optional: build and push a real image

If you prefer a pre-baked image over the ConfigMap mount:

```bash
cd samples/simulator
docker build -t registry.example.com/oom-simulator:1.0 .
docker push registry.example.com/oom-simulator:1.0
```

Then in each pattern YAML, replace:

```yaml
image: python:3.12-alpine
command: ["python", "-u", "/app/app.py"]
volumeMounts: [...]
volumes: [...]
```

with:

```yaml
image: registry.example.com/oom-simulator:1.0
```

and drop the ConfigMap volume/mount.

## Safety

These workloads intentionally trigger OOMKills. Keep them in the `oom-test` namespace; apply a ResourceQuota if needed:

```yaml
apiVersion: v1
kind: ResourceQuota
metadata: { name: oom-test-quota, namespace: oom-test }
spec:
  hard:
    requests.memory: 2Gi
    limits.memory:   4Gi
    pods:            "10"
```

Do **not** schedule these on nodes that host production workloads — they consume real RAM, and chronic OOMs waste kubelet cycles.
