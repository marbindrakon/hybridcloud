# Onboard a tenant

For the platform team. This guide takes a new customer from nothing to people signed in and
working in their tenant. It uses the tenant `example-corp`; the samples in
[samples/](../../samples/README.md) contain the same objects.

You need cluster-admin on the services cluster (or the platform admin role) and admin access to
the tenants' realm of the sign-in directory.

## 1. Decide which sites the tenant may use

A site (`CloudInfrastructure` in `sovereign-cloud`) lists the tenants that may use it in
`spec.entityRefs`; an empty list means every tenant. Sites are managed in Git with the platform's
GitOps values, so add the tenant there and let Argo CD sync it.

A per-tenant allow-list on the tenant itself, and a tenant profile that shows tenants their
sites, are not available yet. Tell the tenant which site names to use.

## 2. Create the tenant

Create the `Entity` in the platform namespace `sovereign-cloud`, from the **Tenants** page of the
admin perspective or with `oc apply`:

```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: Entity
metadata:
  name: example-corp
  namespace: sovereign-cloud
spec:
  description: Example Corp, a tenant for the samples.
  billingID: example-corp-0001
  namespaceRbac:
    entityAdmin:
      - example-admins
    auditor:
      - example-auditors
```

- **Name**: lowercase letters, digits and `-`, no dots (the sign-in groups use the first dot to
  separate tenant and group) and short enough for `entity-<name>` to be a valid namespace name.
- `billingID` is required and becomes a label on the tenant namespace.
- `namespaceRbac` gives roles to access groups (step 4). You can list access groups before they
  exist; a role whose access groups do not exist yet grants nothing.

Wait until the tenant is ready and its namespace exists:

```bash
oc get entity example-corp -n sovereign-cloud
oc get namespace entity-example-corp
```

## 3. Create the access groups

An access group (`Rbac`) is a named group of people in the tenant. Create it in the tenant
namespace; `spec.config` names the identity provider connection (`oc get rbacconfigs -n
sovereign-cloud-plugins`):

```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: Rbac
metadata:
  name: example-admins
  namespace: entity-example-corp
spec:
  config: example-identity
  description: Tenant admins of Example Corp.
```

Each access group has three spellings. They name the same group:

| Where | Spelling | Example |
|---|---|---|
| Sign-in directory (group path) and `Rbac` `status.group` | `<tenant>/<access group>` | `example-corp/example-admins` |
| OpenShift group, RoleBinding subjects | `<tenant>.<access group>` | `example-corp.example-admins` |
| Roles and access fields of other objects | `<access group>` | `example-admins` |

## 4. Give the access groups roles

Edit `spec.namespaceRbac` of the tenant: each key is a role, each value the access groups that
hold it. The platform creates one Role and RoleBinding per role in the tenant namespace. The
roles (generated from the role definitions; one older key with the same meaning as auditor is
deprecated and left out):

<!-- roles:begin -->
| Role | Key in `spec.namespaceRbac` | Create, change and delete | Read |
|---|---|---|---|
| Tenant admin | `entityAdmin` | OpenStack environments, OpenShift Virtualization environments, networks, network placements, edge connections, AWS environments, OpenShift clusters, KV secrets engines, Vaults, Quay organizations, AAP organizations, app projects, team setups, cluster assignments, access groups | the same |
| Auditor | `auditor` | - | OpenStack environments, OpenShift Virtualization environments, networks, network placements, edge connections, AWS environments, OpenShift clusters, KV secrets engines, Vaults, Quay organizations, AAP organizations, app projects, team setups, cluster assignments, access groups |
| OpenStack environment admin | `cloudOSOAdmin` | OpenStack environments | access groups |
| OpenStack environment viewer | `cloudOSOView` | - | OpenStack environments, access groups |
| VM environment admin | `cloudVirtAdmin` | OpenShift Virtualization environments | access groups |
| VM environment viewer | `cloudVirtView` | - | OpenShift Virtualization environments, access groups |
| Network admin | `networkAdmin` | networks, network placements, edge connections | OpenStack environments, OpenShift Virtualization environments, access groups |
| Network viewer | `networkView` | - | networks, network placements, edge connections, access groups |
| AWS environment admin | `cloudAWSAdmin` | AWS environments | access groups |
| AWS environment viewer | `cloudAWSView` | - | AWS environments, access groups |
| OpenShift cluster admin | `platformOpenshiftAdmin` | OpenShift clusters | OpenStack environments, AWS environments, access groups |
| OpenShift cluster viewer | `platformOpenshiftView` | - | OpenShift clusters, access groups |
| Team setup admin | `teamAdmin` | team setups | cluster assignments, access groups |
| Team setup viewer | `teamView` | - | team setups, cluster assignments, access groups |
| App project admin | `projectAdmin` | app projects | cluster assignments, access groups |
| App project viewer | `projectView` | - | app projects, cluster assignments, access groups |
| Cluster assignment admin | `assignmentAdmin` | cluster assignments | app projects, team setups, access groups |
| Access group admin | `identityAdmin` | access groups | the same |
<!-- roles:end -->

Access to the objects a tenant creates is given on those objects, not here: who may sign in to a
VM environment's site console (`CloudVirt` `spec.toolRbac`), who gets which role on an OpenShift
cluster (`PlatformOpenshift` `spec.toolRbac`) and in the namespaces a cluster assignment creates
(`Assignment` `spec.toolRbac`), and the access lists of Vault, KV secrets engine, AAP and Quay
organizations. Resource-scoped access through the access fields of team setups and app projects
is not available yet.

## 5. Add people to the access groups

Members are managed in the sign-in directory: add each person to the group
`<tenant>/<access group>` in the tenants' realm. Tenants cannot change membership themselves yet,
so this is a request to the platform team.

## 6. Check that sign-in works

Ask one member to sign in to the services cluster with **sovereign-services** (step 2 of
[Getting started](../tenant/getting-started.md)). After their first sign-in:

```bash
oc get group example-corp.example-admins
oc auth can-i create hybridnetworks -n entity-example-corp \
  --as=<user> --as-group=example-corp.example-admins
```

The first command shows the person in the group; the second answers `yes` for a role that may
create networks. On an OpenShift Virtualization site, members sign in with **sovereign-tenants**,
and only the access groups an environment lists in `spec.toolRbac` see its namespaces.

## 7. Hand over

Send the tenant admin [Getting started](../tenant/getting-started.md), the tenant name, the site
names they may use, the services apps domain and the tenant DNS domain.

## Offboarding

Delete the tenant's objects in order (placements, then environments and networks, then
clusters and platform services, then access groups), then the `Entity`. Objects with
`spec.deletionProtection: true` stop the tenant delete until protection is turned off; a waiting
delete says what it waits for in its status.
