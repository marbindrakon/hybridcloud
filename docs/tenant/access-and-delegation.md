# Access and delegation

Who may do what in your tenant, and how a tenant admin hands parts of it to a team. The examples
use the tenant `example-corp`.

## The three pieces

- **Access groups** hold people. Each access group is an object in your tenant namespace
  (`oc get rbacs -n entity-<tenant>`, short name `rb`); its members are the members of the group
  `<tenant>/<access group>` in the sign-in directory. Adding and removing members is done by the
  platform team today (a request to them); self-service membership is not available yet.
- **Roles on the tenant** say what an access group may do anywhere in the tenant: for example
  network admin, VM environment admin, tenant admin or auditor. They are set on the tenant by the
  platform team ([the role table](../platform/onboard-a-tenant.md#4-give-the-access-groups-roles)).
- **Access on one object** gives an access group rights on that object only. There are two kinds:
  access to what the object creates elsewhere (`spec.toolRbac`), and access to the object itself
  for team setups and app projects (their access fields, below).

## Access to what an object creates

These lists say who gets in on the site or cluster an object creates. They grant nothing on the
object in your tenant namespace.

| Object | Field | Who gets what |
|---|---|---|
| VM environment | `spec.toolRbac` | Members may sign in to the site's console and work in the environment's namespaces. |
| OpenShift cluster | `spec.toolRbac` | Roles on the tenant's cluster. |
| Cluster assignment | `spec.toolRbac` | Roles in the namespaces the assignment creates on the tenant's clusters. |
| Vault, KV secrets engine, AAP organization, Quay organization | their access lists | Sign-in and roles in that service. |

## Team setups and app projects: delegated access (available with this release)

A tenant admin can let a team manage its own app projects without making the team tenant-wide
admins. The access fields of a team setup and an app project list access groups of the tenant;
each list grants rights on that object (and, for a team setup, on the objects it names), on the
services cluster only.

| Field | On a | The access groups listed may |
|---|---|---|
| `teamAdmin` | team setup | view and change the team setup, including its three access lists, and the app projects its cluster assignments list; view those cluster assignments |
| `teamMemberRbac` | team setup | view the team setup; view and change those app projects |
| `teamViewerRbac` | team setup | view the team setup, those app projects and those cluster assignments |
| `projectAdmin` | app project | view, change and delete the app project |
| `projectDeveloperRbac` | app project | view and change the app project |
| `projectViewerRbac` | app project | view the app project |

Rules that follow from this:

- Nobody gets **create** from these fields: creating a team setup, app project or cluster
  assignment needs a role on the tenant.
- **Changing an access list**: a team setup's three lists (`teamAdmin` included) are managed by
  its `teamAdmin` groups; otherwise only people allowed to delete the team setup (team setup admin
  or tenant admin) can change them. An app project's lists can be changed by people allowed to
  delete it, which includes its `projectAdmin` groups.
- **Cluster assignments are view-only** through a team setup: changing one changes who gets into
  the tenant's clusters, which stays with the cluster assignment admin role.
- **Nothing on clusters**: the fields grant no access on the tenant's OpenShift clusters. That is
  `spec.toolRbac` of the cluster assignment.
- These fields are the interim form of a resource-scoped role assignment. A later release replaces
  them with role assignments; the access they grant stays the same.

Platform roles (Platform administrator, Platform viewer) are not tenant roles: they come from
the platform team. The admin dashboard opens only for the Platform administrator role; a tenant
account that signs in there sees a page naming that role and saying that the platform team adds
members. It shows no tenant contacts.

People with access through these fields see the objects by name, not in a tenant-wide list:
`oc get team <name> -n entity-<tenant>` works, `oc get teams -n entity-<tenant>` is refused. The
dashboards' list pages show them with the line "You may not list all of these; ... shared with
you".

## Deleting a team setup or an app project

Deleting a team setup or an app project removes the access its lists grant and releases its
sign-in groups on the services cluster. The delete dialog lists, before you confirm:

- the access groups whose access ends, with the list each one is on;
- for a team setup, the cluster assignments that name it and the app projects of those
  assignments. They are **not** deleted: the assignments wait until a team setup with that name is
  ready again, and the access the team setup's lists gave on those app projects ends;
- for an app project, the cluster assignments that list it (not deleted; they wait until an app
  project with that name is ready again) and the team setups whose lists reached it.

You type the name of the team setup or app project to confirm. Neither owns memberships in a tool
organization (AAP, Quay, Vault): those come from the tool objects' own access lists and stay.

## Checking access

```bash
oc auth can-i create hybridnetworks -n entity-<tenant>       # a tenant role
oc auth can-i update teams/<name> -n entity-<tenant>         # an access field of a team setup
```

To list everything your role reads without `Forbidden` lines, use the command for your role in
[listing your objects](glossary.md#listing-your-objects).

## Group spelling

One access group, three spellings:

| Where | Spelling |
|---|---|
| Sign-in directory (group path), `status.group` of the access group | `<tenant>/<access group>` |
| OpenShift group, role binding subjects | `<tenant>.<access group>` |
| Roles, `spec.toolRbac` and the access fields | `<access group>` |
