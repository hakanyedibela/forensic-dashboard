#!/usr/bin/env bash
set -euo pipefail

NS="${NS:-monitoring}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ">>> Using namespace: ${NS}"
echo ">>> Manifests from: ${HERE}"

command -v helm >/dev/null   || { echo "helm not found"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl not found"; exit 1; }

THANOS="${THANOS:-0}"

echo ">>> Adding Helm repos"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
if [[ "${THANOS}" == "1" ]]; then
  helm repo add bitnami https://charts.bitnami.com/bitnami >/dev/null 2>&1 || true
fi
helm repo update

echo ">>> Ensuring namespace ${NS}"
kubectl get ns "${NS}" >/dev/null 2>&1 || kubectl create ns "${NS}"

echo ">>> (Optional) create Slack secret if SLACK_WEBHOOK_URL is set"
if [[ -n "${SLACK_WEBHOOK_URL:-}" ]]; then
  kubectl -n "${NS}" create secret generic alertmanager-slack \
    --from-literal=url="${SLACK_WEBHOOK_URL}" \
    --dry-run=client -o yaml | kubectl apply -f -
else
  echo "    (skipped — set SLACK_WEBHOOK_URL env var to enable Slack)"
fi

if [[ "${THANOS}" == "1" ]]; then
  echo ">>> Thanos enabled — preparing object-store secret + bitnami/thanos (with bundled MinIO)"

  if ! kubectl -n "${NS}" get secret thanos-objstore-config >/dev/null 2>&1; then
    echo "    creating secret thanos-objstore-config (points at bundled MinIO)"
    cat <<'EOF' | kubectl -n "${NS}" create secret generic thanos-objstore-config --from-file=objstore.yml=/dev/stdin
type: s3
config:
  bucket: thanos
  endpoint: thanos-minio.monitoring.svc.cluster.local:9000
  access_key: admin
  secret_key: ChangeMe123!
  insecure: true
EOF
  else
    echo "    secret thanos-objstore-config already exists — keeping it"
  fi

  echo ">>> Installing Thanos (Querier + Store + Compactor + bundled MinIO)"
  helm upgrade --install thanos bitnami/thanos \
    -n "${NS}" -f "${HERE}/helm/values-thanos.yaml"

  echo ">>> Installing kube-prometheus-stack (with Thanos sidecar)"
  helm upgrade --install kps prometheus-community/kube-prometheus-stack \
    -n "${NS}" \
    -f "${HERE}/helm/values-kps.yaml" \
    -f "${HERE}/helm/values-kps-thanos.yaml"
else
  echo ">>> Installing kube-prometheus-stack"
  helm upgrade --install kps prometheus-community/kube-prometheus-stack \
    -n "${NS}" -f "${HERE}/helm/values-kps.yaml"
fi

echo ">>> Installing Loki (single-binary)"
helm upgrade --install loki grafana/loki \
  -n "${NS}" -f "${HERE}/helm/values-loki.yaml"

echo ">>> Installing Promtail"
helm upgrade --install promtail grafana/promtail \
  -n "${NS}" \
  --set "config.clients[0].url=http://loki:3100/loki/api/v1/push"

echo ">>> Applying datasources, rules, dashboards"
kubectl apply -f "${HERE}/manifests/loki-datasource.yaml"
kubectl apply -f "${HERE}/manifests/prometheusrules-custom.yaml"
kubectl apply -f "${HERE}/manifests/oom-alerts.yaml"
if [[ "${THANOS}" == "1" ]]; then
  kubectl apply -f "${HERE}/manifests/thanos-datasource.yaml"
fi
kubectl apply -f "${HERE}/dashboards/"

echo ">>> Waiting for Grafana to be ready"
kubectl -n "${NS}" rollout status deploy/kps-grafana --timeout=5m || true

cat <<EOF

========================================================
Install complete.

Access Grafana:
  kubectl -n ${NS} port-forward svc/kps-grafana 3000:80
  http://localhost:3000   user: admin   pass: ChangeMe123!

Access Prometheus:
  kubectl -n ${NS} port-forward svc/kps-kube-prometheus-stack-prometheus 9090

Access Alertmanager:
  kubectl -n ${NS} port-forward svc/kps-kube-prometheus-stack-alertmanager 9093
$(if [[ "${THANOS}" == "1" ]]; then cat <<TEOF

Access Thanos:
  kubectl -n ${NS} port-forward svc/thanos-query 9090       # http://localhost:9090
  kubectl -n ${NS} port-forward svc/thanos-minio 9001:9001  # MinIO console: http://localhost:9001 (admin / ChangeMe123!)

Verify the sidecar is uploading to object storage:
  kubectl -n ${NS} logs -l app.kubernetes.io/name=prometheus -c thanos-sidecar --tail=50
  # In the Querier UI → Stores: one Sidecar entry + one Store Gateway entry
TEOF
fi)

Dashboards:
  - OOMKilled — Detail + Drilldown                [uid: oom-7d-detail]
  - OOMKilled — Combined (Overview + Forensics)   [uid: oom-combined]
  - OOMKilled — Thanos Deep Dive                  [uid: oomkilled-thanos]
  - OOM Forensics — Metrics + Logs + Network      [uid: oom-forensics]
  - OOM — Lean (OpenShift)                        [uid: oom-lean]
  - Loki — Log Overview
  - (plus default kube-prometheus-stack dashboards)

Next steps:
  - Change the Grafana admin password in helm/values-kps.yaml
  - Create alertmanager-slack / alertmanager-smtp secrets
    (see manifests/alertmanager-secrets.md)
  - If using a non-default StorageClass, edit values-*.yaml
$(if [[ "${THANOS}" != "1" ]]; then echo "  - Want long-term metrics? rerun: THANOS=1 ./install.sh"; \
   else echo "  - To use real S3/GCS/Azure instead of bundled MinIO, follow manifests/thanos-objstore.md"; fi)
========================================================
EOF
