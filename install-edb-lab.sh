#!/usr/bin/env bash
# EDB Postgres for Kubernetes monitoring lab on a local kubeadm cluster:
#   kube-prometheus-stack (lab overlay) + EDB operator 1.27.1 (public quay image)
#   + 2-instance cluster with S3 backups (SeaweedFS) + alerts + Grafana dashboards.
#
# Usage: KUBECONFIG=~/.kube/kubeadm_config ./install-edb-lab.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
MON_NS=monitoring
LAB_NS=edb-lab
KPS_CHART_VERSION="${KPS_CHART_VERSION:-92.13.1}"
EDB_VERSION="${EDB_VERSION:-1.27.1}"

echo ">>> kube-prometheus-stack ${KPS_CHART_VERSION}"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update prometheus-community >/dev/null
# helm never upgrades CRDs; apply the chart's CRDs first so the operator matches them.
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
helm pull prometheus-community/kube-prometheus-stack --version "$KPS_CHART_VERSION" -d "$tmp" --untar
kubectl apply --server-side --force-conflicts -f "$tmp/kube-prometheus-stack/charts/crds/crds/" >/dev/null
kubectl create namespace "$MON_NS" --dry-run=client -o yaml | kubectl apply -f -
helm upgrade --install kps prometheus-community/kube-prometheus-stack -n "$MON_NS" \
  --version "$KPS_CHART_VERSION" --skip-crds \
  -f "$HERE/helm/values-kps.yaml" -f "$HERE/helm/values-kps-lab.yaml" --wait --timeout 10m

echo ">>> EDB operator ${EDB_VERSION} (quay.io mirror, implicit trial license)"
curl -sfL "https://get.enterprisedb.io/pg4k/pg4k-${EDB_VERSION}.yaml" \
  | sed "s#docker.enterprisedb.com/k8s/edb-postgres-for-kubernetes:${EDB_VERSION}#quay.io/enterprisedb/cloud-native-postgresql:${EDB_VERSION}#g" \
  | kubectl apply --server-side --force-conflicts -f - >/dev/null
kubectl -n postgresql-operator-system rollout status deploy/postgresql-operator-controller-manager --timeout=5m

echo ">>> Secrets (generated, never written to the repo)"
kubectl create namespace "$LAB_NS" --dry-run=client -o yaml | kubectl apply -f -
if ! kubectl -n "$LAB_NS" get secret edb-s3-creds >/dev/null 2>&1; then
  ak="edb$(openssl rand -hex 6)"; sk="$(openssl rand -hex 24)"
  kubectl -n "$LAB_NS" create secret generic edb-s3-creds \
    --from-literal=ACCESS_KEY_ID="$ak" --from-literal=ACCESS_SECRET_KEY="$sk"
  kubectl -n "$LAB_NS" create secret generic seaweedfs-s3-config --from-literal=s3.json="$(printf \
    '{"identities":[{"name":"edb","credentials":[{"accessKey":"%s","secretKey":"%s"}],"actions":["Admin","Read","Write","List","Tagging"]}]}' "$ak" "$sk")"
fi

echo ">>> S3 store, cluster, backups, PodMonitors"
kubectl apply -f "$HERE/manifests/edb-lab/edb-lab.yaml"
kubectl -n "$LAB_NS" rollout status deploy/seaweedfs --timeout=5m
# Bucket must exist before the first WAL is archived.
kubectl -n "$LAB_NS" exec deploy/seaweedfs -- sh -c \
  'for i in $(seq 1 30); do
     echo "s3.bucket.create -name edb-backups" | weed shell -master=localhost:9333 >/dev/null 2>&1
     echo "s3.bucket.list" | weed shell -master=localhost:9333 2>/dev/null | grep -q edb-backups && exit 0
     sleep 2
   done; echo "bucket edb-backups not created" >&2; exit 1'
kubectl -n "$LAB_NS" wait cluster/pg-lab --for=condition=Ready --timeout=10m

echo ">>> Alerts + dashboards"
kubectl apply -f "$HERE/manifests/edb-postgres-alerts.yaml"
for f in dashboard-edb dashboard-edb-backups dashboard-edb-alerts; do
  kubectl -n "$MON_NS" create configmap "$f" --from-file="$f.json=$HERE/dashboards/$f.json" \
    --dry-run=client -o yaml \
    | kubectl label --local -f - grafana_dashboard=1 -o yaml \
    | kubectl apply -f -
done

cat <<EOF

Done.
  Grafana:    kubectl -n $MON_NS port-forward svc/kps-grafana 3000:80   (admin / see values-kps.yaml)
  Prometheus: kubectl -n $MON_NS port-forward svc/kps-prometheus 9090:9090
  Cluster:    kubectl -n $LAB_NS get cluster,backup,scheduledbackup
EOF
