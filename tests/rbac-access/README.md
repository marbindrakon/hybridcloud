# RBAC access matrix

The Kubernetes side of tenant access is checked by a runner, not by hand: `sar_matrix.py` asks the API server with
SubjectAccessReviews whether a user or a group may do something. A SubjectAccessReview names the user and the groups
explicitly, so no real user, no impersonation and no change on the cluster is needed; it is safe against the live
services cluster.

## Run

```
# live (current KUBECONFIG): the tenant's Entity is read from the cluster
python3 -I tests/rbac-access/sar_matrix.py --entity <tenant> [--other-entity <another tenant>] \
  [--profile generated --profile negative ...] [--users user1,user2] [--report out.json]

# throwaway API server (envtest, see tests/schema-replay/README.md): the tenant's Roles and RoleBindings are created
# by the production task file first, so the positive rows are proven without touching a cluster
KUBEBUILDER_ASSETS=<envtest bin dir> python3 -I tests/rbac-access/sar_matrix.py --cluster envtest --render-roles \
  --entity <name> --entity-spec <file with spec.namespaceRbac> [--automation-rules <rules.yaml>]

make sar-matrix ENTITY=<tenant> [PROFILE=negative]
```

Exit status 1 = at least one unexpected allow or deny. Rows listed in `known-deviations.yaml` (`{id, finding, note}`)
are reported as `known` while they fail and as `fixed` once they pass.

## Rows

| Source | What it checks |
|---|---|
| generated | Every access group `<entity>.<rbac>` mapped in `Entity.spec.namespaceRbac` gets exactly the verbs of its roles (`eda/rulebooks/roles/entity_provision/vars/main.yml`, `entity_named_roles`: crud = get, list, watch, create, update, patch, delete; read = get, list, watch; `<kind>/status` read) plus the tenant baseline of `eda/common/vars/scope_levels.yml` (`tenantprofiles` get, list, watch) in `entity-<entity>`, and nothing else of the `hybridsovereign.redhat` API group there. |
| `cases/negative.yaml` | A signed-in user with no role sees no SHC object in the tenant, platform and plugins namespaces; no access group reads Secrets, creates or execs into pods, writes RoleBindings, escalates Roles, binds admin/cluster-admin, edits its namespace or reads Entities; a tenant's admins get nothing in another tenant. |
| `cases/automation.yaml` | The platform automation account (`system:serviceaccount:open-cluster-management-agent-addon:shc-automation`) can do what the EDA jobs do on services and cannot create ClusterRoleBindings, bind cluster-admin, touch nodes, escalate Roles or exec into pods. |
| `cases/<other>.yaml` | Added by the streams that own those rows (tenant profile, scope roles, security closures). |
| `--users` | The generated rows again, for real users with the groups the cluster's OpenShift Groups give them (read-only). |

Case format (YAML list):

```yaml
- id: tenant-secrets                       # unique; printed in reports and used by known-deviations.yaml
  resolves: [I-10]                         # finding ids
  principal: {user: optional, groups: ["{group:*}", system:authenticated]}
  namespace: entity-{entity}               # omit for cluster-scoped resources
  resource: {group: "", resource: secrets, subresource: optional, name: optional}
  verbs: [get, list]
  expect: deny                             # allow | deny
```

Placeholders: `{entity}`, `{other_entity}`, `{automation}`, `{group:<spec_key>}` (the first access group mapped to that
key; the case is skipped when none is), `{group:*}` in the principal's groups (one case per mapped access group), and
resource `@shc-kinds` (one case per kind of the API group).

## Context: personas

## User Personas

| Persona | Keycloak User (example) | Group / Role Binding | Cluster Access |
|---------|-------------------------|----------------------|------------------|
| **Platform Admin** | `test-platform-admin` | `hybridsovereign-platform-admins` | `cluster-admin` or equivalent on both clusters |
| **Entity Admin** | `test-entity-admin` | `acme-entity-admin` (via Rbac CR) | `edit` in `entity-acme-corp` |
| **Entity Viewer** | `test-entity-viewer` | `acme-auditor` | `view` in `entity-acme-corp` |
| **Team Admin** | `test-team-lead` | `acme-team-admin` + team membership | `edit` on Team CR + related resources |
| **Team Member** | `test-developer` | Team `spec.members` | Team viewer RoleBinding only |
| **Vault Admin** | `test-vault-admin` | `acme-vault-admins` | Vault UI policy (not K8s cluster-admin) |
| **AAP Executor** | `test-aap-executor` | `acme-aap-executors` | AAP launch; no CR create |
| **Tenant Dashboard User** | `test-tenant-user` | Entity-scoped group | OAuth to tenant dashboard; namespace-scoped API |

---


## Not covered by the runner


| Action | Admin Dashboard | Tenant Dashboard | OCP Console Plugin |
|--------|----------------|------------------|-------------------|
| List all entities | ✅ Platform Admin | ❌ | ✅ Admin plugin |
| View own entity CRs | ✅ | ✅ Entity Admin/Viewer | ✅ Tenant plugin |
| Create Team | ✅ | ✅ Entity Admin | ✅ (scoped) |
| Delete Entity | ✅ Platform Admin | ❌ | ❌ |
| View secrets | ❌ | ❌ | ❌ |
| Launch AAP job template | ✅ | ✅ AAP Executor | ❌ |

---

## Vault Policy Matrix

| Path | Platform Admin | Entity Admin | Vault Admin | Tenant User |
|------|---------------|--------------|-------------|-------------|
| `central/*` | ✅ (policy) | ❌ | ✅ | ❌ |
| `entity-acme-corp/*` | ✅ | ✅ (scoped) | ✅ | ❌ |
| `entity-globex/*` | ✅ | ❌ | ✅ | ❌ |

---

