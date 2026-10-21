# Demo tenant: acme-corp

[acme-corp.yaml](acme-corp.yaml) is a complete small tenant: the tenant itself, five access groups, the
tenant-wide role each group holds, and one team setup and one app project whose access lists give a group
access to just those objects. The platform seeds it (the operator-samples job applies the same file); read it
to see how the pieces fit together.

It is not part of an "apply everything": the tenant object is platform-owned, and the access groups must be
ready before the roles that name them can be bound.

## Apply order

1. The tenant (Entity `acme-corp` in the platform namespace). This creates the tenant namespace
   `entity-acme-corp`.
2. The five access groups (Rbac) in `entity-acme-corp`. Wait until each one is ready
   (`oc get accessgroups -n entity-acme-corp`).
3. The tenant again, now with `spec.namespaceRbac`, then the team setup, the app project and the AAP and
   Quay organizations.

Applying everything at once also works: when an access group becomes ready, the platform refreshes the tenant,
team setups and app projects that name it.

## Who may do what

| Access group | Tenant-wide role (`spec.namespaceRbac`) | Through the access lists |
|---|---|---|
| `platform-admins` | Tenant admin, access group admin | AAP and Quay organization admin |
| `infra` | VM environment admin, OpenStack environment admin, network admin | - |
| `editors` | VM environment, OpenStack environment and network viewer | Team setup `platform-engineering`: view and change; app project `api-services`: view, change and delete (including its access lists); AAP job executor; Quay creator |
| `viewers` | Auditor (read everything) | Team setup `platform-engineering`: view; AAP and Quay viewer/member |
| `developers` | none | Team setup `platform-engineering`: view; app project `api-services`: view and change; Quay member |

`developers` holds no tenant-wide role: its members see the tenant namespace, the tenant profile
(`oc get tprofile shc -n entity-acme-corp`), the team setup and the app project, and nothing else. An unfiltered
`oc get teams` is refused for them; `oc get teams --field-selector metadata.name=platform-engineering` works. In
`oc`, app projects are `proj` (`oc get proj -n entity-acme-corp`): `projects` names OpenShift projects.

The team setup has no cluster assignment (there is no tenant cluster in the demo), so its access lists reach only
the team setup itself; with a cluster assignment they also reach the app projects it lists.

## Members

The platform's test users are members of `/acme-corp/<access group>` in the identity provider; each signs in to
the services cluster with the group `acme-corp.<access group>`.
