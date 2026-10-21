# Day-2 Operations: Custom Operators

## Overview

Sovereign Cloud deploys custom Ansible-based operators via the services cluster. This guide covers creating and managing Custom Resources (CRs), upgrading operators, tuning reconciliation, and monitoring.

All operators use the API group `hybridsovereign.redhat`, version `v1alpha1`.

| Operator | Namespace | CRDs |
|----------|-----------|------|
| Entity | sovereign-cloud | Entity |
| Team | sovereign-cloud | Team |
| Assignment | sovereign-cloud | Assignment |
| Project | sovereign-cloud | Project |
| PlatformOpenshift | sovereign-cloud | PlatformOpenshift |
| CloudOSO | sovereign-cloud | CloudOSO |
| Plugin RBAC | sovereign-cloud-plugins | RbacConfig, Rbac |
| Plugin Vault | sovereign-cloud-plugins | Vault, VaultKV |
| Plugin AAP | sovereign-cloud-plugins | AAPConfig, AAPOrg |
| Plugin Quay | sovereign-cloud-plugins | QuayConfig, QuayOrg |
| Plugin SDX | sovereign-cloud-plugins | Iaac |

---

## 1. Creating Custom Resources

### Entity

```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: Entity
metadata:
  name: acme-corp
  namespace: sovereign-cloud
spec:
  description: "Acme Corporation tenant"
  billingId: "ACME-001"
  websiteLink: "https://acme.example.com"
```

```bash
oc apply -f entity-acme-corp.yaml
oc get entity acme-corp -n sovereign-cloud -o yaml | grep -A 10 status
```

The Entity operator creates a namespace `entity-acme-corp` with the required `hybridsovereign.redhat/entity` label.

### Team

```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: Team
metadata:
  name: platform-team
  namespace: entity-acme-corp    # must be in the entity namespace
spec:
  features:
    istio: false
    argo: false
  rbacConfig: default-rbac-config  # RbacConfig name in sovereign-cloud-plugins
  teamAdmin:
    - admin-rbac                   # Rbac CR name
```

### Assignment

```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: Assignment
metadata:
  name: acme-platform-assignment
  namespace: entity-acme-corp
spec:
  team: platform-team
  projects:
    - web-project
  openshift:
    - dev-cluster
```

### Project

```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: Project
metadata:
  name: web-project
  namespace: entity-acme-corp
spec:
  rbacConfig: default-rbac-config
  projectAdmin:
    - admin-rbac
```

---

## 2. Checking CR Status

```bash
# Overview
oc get entity,team,assignment,project -n entity-acme-corp

# Detailed status
oc describe assignment acme-platform-assignment -n entity-acme-corp

# Watch reconciliation events
oc get events -n entity-acme-corp --field-selector reason=Reconciling --watch
```

A healthy CR will show `ready: "True"` in `.status`.

---

## 3. Operator Upgrade

Operator images are pushed to the OCI registry and deployed via ArgoCD. To upgrade:

### Step 1: Build and push the new image

```bash
# From the operator directory (e.g., Team/)
export OPERATOR_IMAGE_TAG=0.0.3
make docker-build docker-push
```

### Step 2: Push the new Helm chart

```bash
# Bump version in helm/Chart.yaml (appVersion + version)
# Then from bootstrap/
make upload-team-operator-chart
```

### Step 3: Update values.yaml and deploy

Update `bootstrap/helm/central/values.yaml`:
```yaml
teamOperator:
  chartVersion: "0.3.4"   # new chart version
```

Commit and push to Git — ArgoCD will sync automatically.

### Step 4: Verify rollout

```bash
oc rollout status deployment team-operator -n sovereign-cloud
oc get pod -n sovereign-cloud -l name=team-operator
```

---

## 4. Reconciliation Tuning

All operators are configured with `maxConcurrentReconciles: 10` in `operator/watches.yaml`.

```yaml
# watches.yaml
- version: v1alpha1
  group: hybridsovereign.redhat
  kind: Team
  role: roles/team
  reconcilePeriod: 30s
  maxConcurrentReconciles: 10
```

### Adjusting concurrency

If operators are consuming too much CPU:
1. Lower `maxConcurrentReconciles` (minimum: 1)
2. Increase `reconcilePeriod` to reduce re-queue frequency

If operators are falling behind on a large number of CRs:
1. Increase `maxConcurrentReconciles` up to 20
2. Consider increasing memory limits for stateful operators (plugin_vault, CloudOSO)

Memory guidelines:
- Stateful operators (CloudOSO, Plugin Vault): 2Gi limit, 512Mi request
- Standard operators: 1Gi limit, 256Mi request

---

## 5. Operator Metrics and Alerts

Operators expose Prometheus metrics at port `8443` via HTTPS.

```bash
# Check ServiceMonitor is picked up
oc get servicemonitor -n sovereign-cloud

# Query metrics (from within cluster or via port-forward)
oc port-forward -n sovereign-cloud svc/team-operator-metrics 8443:8443
curl -sk https://localhost:8443/metrics | grep reconcile
```

### Key metrics

| Metric | Description |
|--------|-------------|
| `controller_runtime_reconcile_total` | Total reconciliations by result (success/error) |
| `controller_runtime_reconcile_errors_total` | Reconciliation errors |
| `controller_runtime_active_workers` | Current active reconcile goroutines |
| `controller_runtime_max_concurrent_reconciles` | Configured max concurrency |

### Alert rules

Each operator has two PrometheusRule alerts:
- `<Kind>ReconcileErrors` — fires when error rate > 0 for 5 minutes
- `<Kind>OperatorDown` — fires when no pods are reporting metrics

See [26-observability.md](../technical/26-observability.md) for the full alert table.

---

## 6. Troubleshooting

### CR stuck in reconciling

```bash
# Check operator logs
oc logs -n sovereign-cloud -l name=team-operator --tail=100 | grep -i error

# Check if entity namespace has required label
oc get namespace entity-acme-corp -o jsonpath='{.metadata.labels}'
# Must include: hybridsovereign.redhat/entity: acme-corp
```

### Operator crash-looping

```bash
# Check pod events
oc describe pod -n sovereign-cloud -l name=team-operator

# Check resource limits
oc get pod -n sovereign-cloud -l name=team-operator -o jsonpath='{.items[0].spec.containers[0].resources}'
```

### Force re-reconcile

```bash
# Add an annotation to trigger immediate reconciliation
oc annotate team platform-team -n entity-acme-corp \
  force-sync=$(date +%s) --overwrite
```

---

## Related Documentation

- [17-entity-operator.md](../archive/technical/17-entity-operator.md) — Entity operator reference
- [24-tenancy-operators.md](../archive/technical/24-tenancy-operators.md) — Tenancy operators reference
- [26-observability.md](../technical/26-observability.md) — Metrics and alerts
- [27-operator-performance.md](../technical/27-operator-performance.md) — Reconcile tuning
