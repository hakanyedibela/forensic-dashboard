# Thanos object-store secret

Both the Prometheus sidecar (in `helm/values-kps-thanos.yaml`) and the Thanos chart (in `helm/values-thanos.yaml`) read the same secret:

- name: `thanos-objstore-config`
- namespace: `monitoring`
- key: `objstore.yml`

The secret must exist **before** the Helm upgrade that enables the sidecar. Without it the Prometheus pod stays in `Init`/`CrashLoopBackOff` (the sidecar refuses to start without object-store config).

`install.sh` creates the bundled-MinIO version automatically when run with `THANOS=1`. Use the snippets below if you create the secret by hand or migrate to a real backend.

## Bundled MinIO (lab / dev)

`helm/values-thanos.yaml` ships MinIO as a subchart, exposed at `thanos-minio.monitoring.svc.cluster.local:9000`. The credentials below match `minio.auth.rootUser` / `minio.auth.rootPassword` in that file — change both together if you alter them.

```bash
cat <<'EOF' | kubectl -n monitoring create secret generic thanos-objstore-config --from-file=objstore.yml=/dev/stdin
type: s3
config:
  bucket: thanos
  endpoint: thanos-minio.monitoring.svc.cluster.local:9000
  access_key: admin
  secret_key: ChangeMe123!
  insecure: true
EOF
```

## Real S3

```bash
cat <<'EOF' | kubectl -n monitoring create secret generic thanos-objstore-config --from-file=objstore.yml=/dev/stdin
type: s3
config:
  bucket: my-thanos-bucket
  endpoint: s3.eu-central-1.amazonaws.com
  region: eu-central-1
  access_key: AKIA...
  secret_key: ...
  insecure: false
EOF
```

For IAM roles for service accounts (IRSA) on EKS, drop `access_key` / `secret_key` and add `aws_sdk_auth: true`; ensure the Prometheus and Thanos service accounts have the `eks.amazonaws.com/role-arn` annotation.

## GCS

```bash
cat <<'EOF' | kubectl -n monitoring create secret generic thanos-objstore-config --from-file=objstore.yml=/dev/stdin
type: GCS
config:
  bucket: my-thanos-bucket
  service_account: |
    {
      "type": "service_account",
      "project_id": "my-project",
      "private_key_id": "...",
      "private_key": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n",
      "client_email": "thanos@my-project.iam.gserviceaccount.com",
      "client_id": "...",
      "auth_uri": "https://accounts.google.com/o/oauth2/auth",
      "token_uri": "https://oauth2.googleapis.com/token"
    }
EOF
```

## Azure Blob Storage

```bash
cat <<'EOF' | kubectl -n monitoring create secret generic thanos-objstore-config --from-file=objstore.yml=/dev/stdin
type: AZURE
config:
  storage_account: mystorageaccount
  storage_account_key: "..."
  container: thanos
EOF
```

## Ceph / on-prem S3-compatible

Same shape as AWS S3, with `endpoint` pointing at the Ceph RGW / MinIO host and `insecure: true` if you're using plain HTTP or self-signed TLS:

```yaml
type: s3
config:
  bucket: thanos
  endpoint: rgw.internal.example.com:7480
  access_key: ...
  secret_key: ...
  insecure: true
  signature_version2: false
```

## Updating the secret

```bash
cat <<'EOF' | kubectl -n monitoring create secret generic thanos-objstore-config \
  --from-file=objstore.yml=/dev/stdin --dry-run=client -o yaml | kubectl apply -f -
type: s3
config:
  ...
EOF

# Roll Prometheus + Thanos pods so they re-read the secret
kubectl -n monitoring rollout restart statefulset/prometheus-kps-kube-prometheus-stack-prometheus
kubectl -n monitoring rollout restart deploy -l app.kubernetes.io/name=thanos
```

## Verification

After Prometheus + Thanos are up, check that the sidecar is uploading:

```bash
# Sidecar logs — look for "uploaded block" lines (one every ~2h once a block ages out)
kubectl -n monitoring logs -l app.kubernetes.io/name=prometheus -c thanos-sidecar --tail=50

# What's in the bucket
kubectl -n monitoring run -it --rm mc --image=minio/mc --restart=Never -- \
  sh -c 'mc alias set local http://thanos-minio:9000 admin ChangeMe123! && mc ls local/thanos/'

# Is Querier seeing all sources?
kubectl -n monitoring port-forward svc/thanos-query 9090
# open http://localhost:9090/stores  → should list one Sidecar entry and one Store Gateway entry
```
