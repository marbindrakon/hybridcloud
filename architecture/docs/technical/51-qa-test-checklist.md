# Platform QA Test Checklist

Manual and automated validation checklist for the Hybrid Sovereign Cloud platform.
Automated checks live in `global_tests/`; run `ansible-playbook playbooks/validate-all.yml`
from that directory with required OCP environment variables set.

## Pre-Deployment Checks

- [ ] Environment variables set (`OCP_CENTRAL_SERVER`, `OCP_CENTRAL_USERNAME`, `OCP_CENTRAL_PASSWORD`, `OCP_SERVICES_SERVER`, `OCP_SERVICES_USERNAME`, `OCP_SERVICES_PASSWORD`)
- [ ] Central cluster reachable: `oc login $OCP_CENTRAL_SERVER`
- [ ] Services cluster reachable: `oc login $OCP_SERVICES_SERVER`
- [ ] ArgoCD accessible at `https://openshift-gitops-server-openshift-gitops.apps.central.lab.example.com/`
- [ ] Vault accessible at `https://vault-central.apps.central.lab.example.com/`
- [ ] Services Vault accessible at `https://vault-services.apps.services.lab.example.com/`
- [ ] Keycloak SSO accessible at its public address (`KEYCLOAK_EXTERNAL_URL`, for example `https://auth.example.com/`)
- [ ] OCI registry credentials verified (`make check-env` from bootstrap repo)

## Infrastructure Checks

- [ ] All ArgoCD Applications: Synced + Healthy (`oc get applications -n openshift-gitops --context central-admin`)
- [ ] All sovereign-cloud pods: Running (`oc get pods -n sovereign-cloud --context services-admin`)
- [ ] All sovereign-cloud-plugins pods: Running (`oc get pods -n sovereign-cloud-plugins --context services-admin`)
- [ ] Vault central: initialized and unsealed (`GET /v1/sys/health` returns HTTP 200, `sealed=false`)
- [ ] Vault services: initialized and unsealed
- [ ] Keycloak: `sovereign-tenants` realm OIDC discovery returns HTTP 200
- [ ] Keycloak pods running in `services-rhbk` namespace
- [ ] Central cluster nodes Ready
- [ ] Services cluster nodes Ready

## Operator Health Checks

- [ ] Entity operator: running and reconciling Entity CRs
- [ ] Team operator: running
- [ ] Assignment operator: running
- [ ] Persona operator: running
- [ ] PlatformOpenshift operator: running
- [ ] CloudOSO operator: running
- [ ] CloudAWS operator: running (or skipped when `aws_excluded=true`)
- [ ] plugin-rbac operator: running in sovereign-cloud-plugins
- [ ] plugin-aap, plugin-quay, plugin-vault, plugin-iaac: running in sovereign-cloud-plugins

## RBAC Integrity Checks

- [ ] Rbac CRs exist in entity namespaces (e.g. `entity-acme-corp`)
- [ ] Each Rbac CR has `status.ready == true`
- [ ] `plugin-rbac-manager` ClusterRoleBinding present on services cluster
- [ ] Entity namespace RoleBindings present (entity-admin, cloudoso-admin, etc.)
- [ ] Persona CRs exist and are ready
- [ ] Team CRs exist and are ready
- [ ] Assignment CRs exist and are ready
- [ ] Keycloak groups created for each Rbac CR (verify via admin API or tenancy dashboard)

## Dashboard Functionality Checks

- [ ] User dashboard route reachable: `https://sovereign-cloud-dashboard-sovereign-cloud.apps.services.lab.example.com/`
- [ ] Tenancy dashboard route reachable: `https://tenancy-dashboard-sovereign-cloud.apps.services.lab.example.com/`
- [ ] User dashboard: login via OAuth works
- [ ] User dashboard: entity list loads
- [ ] User dashboard: operators page shows all operators
- [ ] Tenancy dashboard: entity namespace selector works
- [ ] Tenancy dashboard: Team list loads
- [ ] EDA job chip URLs use operator-provided `job.url` (expected format: `/execution/jobs/playbook/` on AAP, not `/#/jobs/`)
- [ ] No hardcoded cluster URLs in dashboard source (URLs built from route CRs or CR status)

## Cluster Build Verification

- [ ] `ocp-ses10`: `status.clusterHealth == Healthy`
- [ ] `ocp-ses10`: `status.consoleURL` reachable
- [ ] `ocp-ses10`: `status.clusterVersion` populated
- [ ] `ocp-ses4`: `status.clusterHealth == Healthy`
- [ ] `ocp-ses4`: `status.consoleURL` reachable
- [ ] `ocp-ses4`: `status.clusterVersion` populated
- [ ] CloudOSO CR (`ses4-env`): status ready
- [ ] CloudAWS CR (`ses10-env`): status ready (when AWS enabled)

## EDA / Event Flow Checks

- [ ] Entity create/delete triggers EDA events (automated: `validate-eda.yml`)
- [ ] AAP activations present and active (when AAP token available)
- [ ] EDA job chips appear on dashboard CR list pages

## Automated Test Playbooks

| Playbook | Roles | Purpose |
|----------|-------|---------|
| `validate-all.yml` | All | Full platform validation + HTML report |
| `validate-infrastructure.yml` | check_argocd, check_nodes | ArgoCD apps and cluster nodes |
| `validate-operators.yml` | check_operators | Operator pod health |
| `validate-services.yml` | check_vault_connectivity, check_keycloak_oidc | Vault and Keycloak endpoints |
| `validate-tenancy.yml` | check_personas, check_rbac_bindings, check_cluster_builds | Tenancy CRs and cluster builds |
| `validate-eda.yml` | check_eda_events | Entity create/delete EDA flow |
| `validate-dashboards.yml` | check_dashboards | Route health probes |

## Sign-Off Table

| Check Category | Pass/Fail | Tester | Date | Notes |
|----------------|-----------|--------|------|-------|
| Pre-deployment | | | | |
| Infrastructure | | | | |
| Operators | | | | |
| RBAC | | | | |
| Dashboards | | | | |
| Cluster Builds | | | | |
| EDA / Events | | | | |

## Known Gaps / Follow-Up

- ArgoCD Applications may not appear when querying wrong cluster context; always use `central-admin`.
- Keycloak admin credentials are in Vault; automated user provisioning tests require Vault KV access.
- EDA activation checks require AAP bearer token (not stored in git).
- Stale pods in `ContainerStatusUnknown` should be cleaned by node/kubelet; filter by `Running` phase for health counts.
- CloudAWS CR may show `failed` when AWS sandbox credentials are unavailable (`aws_excluded=true` in global_tests).
