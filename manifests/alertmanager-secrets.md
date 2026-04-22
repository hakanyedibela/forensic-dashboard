# Alertmanager secrets

The Alertmanager config in `helm/values-kps.yaml` mounts two secrets:

- `alertmanager-slack` — Slack incoming webhook URL
- `alertmanager-smtp`  — SMTP auth password (only if using email)

Create them in the `monitoring` namespace **before** the Helm upgrade, otherwise Alertmanager will fail to start.

## Slack

1. In Slack → Apps → "Incoming Webhooks" → add to a channel → copy webhook URL
2. Create secret:

```bash
kubectl -n monitoring create secret generic alertmanager-slack \
  --from-literal=url='https://hooks.slack.com/services/T00000/B00000/XXXXXXXXX'
```

Update:

```bash
kubectl -n monitoring create secret generic alertmanager-slack \
  --from-literal=url='https://hooks.slack.com/services/NEW' \
  --dry-run=client -o yaml | kubectl apply -f -
```

## SMTP (email)

```bash
kubectl -n monitoring create secret generic alertmanager-smtp \
  --from-literal=password='smtp-app-password'
```

Also adjust `smtp_from`, `smtp_smarthost`, and receivers in `helm/values-kps.yaml`.

## Microsoft Teams

Native receiver doesn't exist — deploy prometheus-msteams as a bridge:

```bash
helm repo add prometheus-msteams https://prometheus-msteams.github.io/prometheus-msteams/
helm upgrade --install prometheus-msteams prometheus-msteams/prometheus-msteams \
  -n monitoring \
  --set connectors[0].alerts='https://outlook.office.com/webhook/XXXX/IncomingWebhook/YYYY'
```

Then add a receiver to `values-kps.yaml`:

```yaml
receivers:
  - name: teams
    webhook_configs:
      - url: http://prometheus-msteams:2000/alerts
        send_resolved: true
```

## End-to-end test (Slack)

```bash
kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-alertmanager 9093 &
curl -XPOST http://localhost:9093/api/v2/alerts -H 'Content-Type: application/json' -d '[
  {"labels":{"alertname":"TestFire","severity":"critical","namespace":"monitoring"},
   "annotations":{"summary":"Test alert from ops"}}
]'
```

You should see the alert land in `#alerts-critical` within a few seconds.

## Not using Slack?

Set `alertmanager.config.route.receiver` to `"null"` in `values-kps.yaml` and remove the `slack_configs` blocks. Alertmanager will start fine without the Slack secret if no receiver references it.
