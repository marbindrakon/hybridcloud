# Sign-in to an OpenStack site

People of a tenant use an OpenStack environment with their platform identity: they sign in to the site's
dashboard through the platform's sign-in directory and work in the environment's OpenStack project. This page
says what the cloud's operator sets up once on the site, what the platform team declares, what the platform
then does for each environment and each OpenShift cluster, and which rights the platform's account on the site
needs.

Terms used on this page:

- **Identity service**: the site's Keystone.
- **Sign-in directory**: the platform's OpenID Connect provider; the tenant realm holds the tenants' people and
  their access groups.
- **Identity provider**: the identity service's record of the sign-in directory (federation). People who sign in
  through it get a user in the identity service, created at their first sign-in.
- **Mapping**: the identity service's rules that turn the claims of a sign-in token into a user and its groups.
- **Group domain**: the domain of the identity service in which the mapping looks the groups up.
- **Application credential**: a secret, made in the identity service, that lets a tool act as its owner in one
  project.

The platform never creates or changes an identity provider, a protocol or a mapping on a site. They are the
operator's.

## How it works

1. A tenant names access groups on the OpenStack environment (`spec.toolRbac`: `editRbac`, `viewRbac`).
2. For each access group in use, the platform makes a group `<tenant>.<access group>` in the site's group domain
   and grants it a role on the environment's project: `member` for edit, `reader` for view. Then it gives the
   access group's members a client role of the same name on the site's client in the sign-in directory.
3. A member signs in to the dashboard. The sign-in token carries the claim `groups` with one entry per access
   group the person is in and that is in use on this site. The identity service maps the entries to its groups,
   and the person sees the projects of those groups.
4. An access group removed from the environment goes the other way round: the client role first, then the
   group's role on the project, then the group (once no other environment of the tenant uses it).

OpenShift clusters in an environment do not use a person's identity. Each cluster gets its own user in the
environment's domain, with `member` on the project, and one application credential that lives as long as the
cluster. See [Cluster users](#cluster-users).

## What the platform team provides to the operator

The installation creates a client `openstack-<site>` in the tenant realm, in its values next to the OpenShift
sites' clients: a confidential client, authorization code flow only, no password grant, the exact redirect URIs of
the site's identity service, and a client-role mapper (`oidc-usermodel-client-role-mapper`, client itself, claim
`groups`, multivalued, in the ID token and the user info). Its secret is written to the platform's secret store.

The platform team gives the operator the issuer URL of the tenant realm, the client id and the secret, over the
operator's own secure channel. The operator gives back the redirect URI of the identity service's web server and
the values of the site declaration below.

## What the operator sets up once

| # | Step | kolla-ansible | Red Hat OpenStack Services on OpenShift 18 |
|---|---|---|---|
| 1 | The identity service's web server becomes an OpenID Connect client of the tenant realm | an entry in `keystone_identity_providers` and `keystone_identity_mappings`, the issuer's metadata files | Secret `keystone-httpd-override` and `httpdCustomization.customConfigSecret`; a second realm needs 18.0.14 or later (`federatedRealmConfig`) |
| 2 | Authorization code flow | `keystone_federation_oidc_response_type: code` (the default `id_token` is refused by the client) | in the web server directives |
| 3 | A domain for the people who sign in, the identity provider with the realm's issuer as remote id, bound to that domain | made by the deploy from the entry | `openstack` command-line client |
| 4 | The mapping below and the protocol `openid` | the mapping file of the entry | command-line client |
| 5 | `authorization_ttl` on the identity provider (see below) | by API after the deploy | `--authorization-ttl` |
| 6 | The dashboard offers the identity provider; the identity service trusts the dashboard's return address | generated: `WEBSSO_CHOICES`, `WEBSSO_IDP_MAPPING`, trusted dashboards | dashboard `customServiceConfig`, `[federation] trusted_dashboard` |
| 7 | `[auth] methods` include `openid` and `application_credential` | generated | `customServiceConfig` |
| 8 | The platform's account gets the rights of [Rights of the platform's account](#rights-of-the-platforms-account) for the chosen group domain | | the domain manager role needs 18.0.21 or later |
| 9 | The group domain holds no group with more than project roles | | |

Do not use a `projects` element in the mapping (it makes role assignments that the identity service never
removes), the singular `group` form (it names one group that does not exist when the claim has more than one
value), or a `user.id` taken from a name (a renamed person would get a new user).

### The mapping

The group domain is a choice of the operator. By default the platform looks for the groups in the site's tenant
domain (`spec.openstack.projectDomain`). The two choices differ in one value of the mapping:

- **Tenant domain** (default, recommended with one tenant domain): every call of the platform stays inside the
  tenant domain, so the domain manager role on that domain is enough.
- **Federation domain** (the domain of the people who sign in): set `spec.openstack.signIn.groupDomain` to it.
  Each grant then crosses domains and needs the `admin` role.

Mapping with the groups in the tenant domain `tenants`:

```json
[
  { "local":  [ { "user": { "id": "{0}", "name": "{1}" } } ],
    "remote": [ { "type": "OIDC-sub" }, { "type": "OIDC-preferred_username" } ] },
  { "local":  [ { "groups": "{0}", "domain": { "name": "tenants" } } ],
    "remote": [ { "type": "OIDC-groups" } ] }
]
```

Mapping with the groups in the federation domain `platform-federated`:

```json
[
  { "local":  [ { "user": { "id": "{0}", "name": "{1}" } } ],
    "remote": [ { "type": "OIDC-sub" }, { "type": "OIDC-preferred_username" } ] },
  { "local":  [ { "groups": "{0}", "domain": { "name": "platform-federated" } } ],
    "remote": [ { "type": "OIDC-groups" } ] }
]
```

The `OIDC-` prefix is the web server's claim prefix; use the one your web server sets. The first rule gives every
person a user, also a person without any group on this site. A group named in the token that does not exist in
the group domain is skipped; the person signs in with the other groups. The tenant domain must exist before the
mapping names it: a mapping that names a domain that does not exist refuses every sign-in.

### authorization_ttl

`authorization_ttl` on the identity provider (minutes) is how long the identity service keeps a person's group
memberships after a sign-in. It decides two things:

- Application credentials of people: a person whose role comes from a mapped group can create one only when the
  value is greater than 0. Such a credential works until `authorization_ttl` minutes after the person's last sign-in
  to the dashboard, stops, and works again after the next sign-in.
- How long a person removed from an access group keeps API access: up to `authorization_ttl` minutes after the
  last sign-in. Dashboard access ends when the person's token expires.

Use a value from 480 to 10080 (eight hours to seven days). A shorter value cuts a removed person off sooner and
makes people sign in more often. With 0, people cannot create application credentials. When the attribute is not
set, the identity service uses its configuration default, which no API reports; declare the value then (below), or
the environments say "How long it works after your last sign-in is set by the site."

## What the platform team declares on the site

```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: CloudInfrastructure
metadata:
  name: site-osp
spec:
  type: openstack
  openstack:
    projectDomain: tenants
    dashboard:
      url: https://dashboard.site-osp.example.org
    signIn:
      identityProvider: platform-tenants
      protocol: openid
      displayName: "Example Platform"
      start: IdentityProvider        # or Dashboard (default)
      authorizationTTLMinutes: 1440  # optional: used when the site does not report the value
      # groupDomain: platform-federated   # optional; default projectDomain
```

- `start: Dashboard`: the environment's console link opens the dashboard's sign-in page; people choose the entry
  `displayName`.
- `start: IdentityProvider`: the link starts sign-in at the identity provider and returns to the dashboard
  (`<identity service>/v3/auth/OS-FEDERATION/identity_providers/<identityProvider>/protocols/<protocol>/websso?origin=<dashboard>/auth/websso/`).
  The dashboard URL must be in the identity service's trusted dashboards. A dashboard other than the OpenStack
  dashboard (Horizon) supports `Dashboard` only.

Without `signIn`, environments on the site offer no sign-in for people; OpenShift clusters are still built there.

## What the platform checks

The site job reads, never writes:

| Check | How | When it cannot |
|---|---|---|
| The identity provider exists, is enabled, its domain and `authorization_ttl` | `GET /v3/OS-FEDERATION/identity_providers/<id>` | declared, not confirmed |
| The protocol and its mapping; the mapping uses `groups` with the group domain, has no `projects`, maps `user.id` | `GET .../protocols/<protocol>`, `GET /v3/OS-FEDERATION/mappings/<id>` | declared, not confirmed |
| The group domain exists; the account's roles | `GET /v3/domains`, `GET /v3/role_assignments?user.id=<account>` | the condition says what is missing |
| The start URL answers with a redirect | one unauthenticated request | a note |
| The client `openstack-<site>` exists in the tenant realm, with its groups mapper | the sign-in directory's admin API | a note |

The result is in the site's `status.site.signIn` (`confirmed`, `notConfirmed`, `warnings`, `account`,
`authorizationTTLMinutes` with `authorizationTTLSource` Site, Declared or Unknown) and the condition
`SignInConfigured`:

| Status / reason | Meaning |
|---|---|
| True / Confirmed | The identity provider, its protocol and mapping were read from the site. |
| True / DeclaredNotConfirmed | Declared by the platform team; the account may not read the site's federation settings (a domain manager may not). |
| True / MappingWarning | The mapping uses a project list, does not look groups up in the group domain, or has no stable user id. Sign-in stays on; the mapping is the operator's. |
| False / NotDeclared | No sign-in is declared for this site. |
| False / IdentityProviderNotFound, GroupDomainNotFound, ClientNotFound | What is missing. Environments on the site offer no sign-in until it is fixed. |

The capabilities `consoleSignIn` and `apiCredentials` (a known `authorization_ttl` greater than 0) reach the
tenants' profiles as `ConsoleSignIn` and `ApiCredentials`.

Some things the platform can never check: the web server's client configuration, the trusted dashboards, the
dashboard's sign-in menu, the configuration default of `authorization_ttl` and the claim delimiter. The first real
sign-in proves them.

## Rights of the platform's account

"Manager" is the domain manager role on the domain, where the identity service has that persona (Keystone 2024.2
or later; Red Hat OpenStack Services on OpenShift 18.0.21 or later). Without it, every row needs `admin`.

| Step | Call | Least role |
|---|---|---|
| Project find, create, delete | `GET/POST/DELETE /v3/projects` | manager |
| The account's own `member` on the project | `PUT /v3/projects/{p}/users/{self}/roles/{r}` | manager, when the account's user is in that domain; else `admin` |
| Cluster user find, create, options, password, delete | `GET/POST/PATCH/DELETE /v3/users` | manager |
| Cluster user `member` on the project | `PUT /v3/projects/{p}/users/{u}/roles/{r}` | manager |
| Cluster user extra roles (`rolesBySite`, for example `load-balancer_member`) | the same | `admin`, or a site policy that lets the domain manager grant the role |
| Group find, create, delete in the group domain | `GET/POST/DELETE /v3/groups` | manager on the group domain |
| Group role on the project, groups in the tenant domain | `PUT/DELETE /v3/projects/{p}/groups/{g}/roles/{r}` | manager (`member`, `reader` are grantable) |
| Group role on the project, groups in a federation domain | the same | `admin` |
| Application credential create, list, delete | `.../v3/users/{u}/application_credentials` | none: always done as the owner (the cluster user) |
| Federation reads | `GET /v3/OS-FEDERATION/...` | `admin`; optional (a domain manager gets 403, so sign-in is "declared, not confirmed") |
| Tenant domain create (`createProjectDomain`) | `POST /v3/domains` | `admin`; optional |

Facts that decide the design: a domain manager cannot delete another user's application credential (403), but can
delete the user, and the user's credentials go with it. A domain manager cannot read identity providers (403). The
site job gives the account `admin` on the tenant domain only when the account already holds `admin`; otherwise it
reports the roles it found.

## Cluster users

Each OpenShift cluster on an OpenStack environment acts as its own user of the environment's domain, named
`shc-<build name>` by default, with `member` on the environment's project. The user:

- has no stored password: to make the cluster's application credential the platform sets a random password, waits
  two seconds, signs in as the user, deletes the user's credentials, creates one restricted application credential
  (it cannot create or delete credentials or change the password) and sets a second random password;
- carries the options `ignore_user_inactivity`, `ignore_password_expiry`,
  `ignore_change_password_upon_first_use`, `ignore_lockout_failure_attempts` and `lock_password`;
- is marked as the platform's in its description; a user of the same name without the mark is refused, not
  adopted;
- is deleted with the cluster, after the cluster's machines and floating IPs are gone; its credential goes with it.

A credential that still signs in is kept: a steady run of the cluster's job sets no password. The credential is in
the platform's secret store and in the cluster (`kube-system/openstack-credentials`). With it, the cluster can
change everything in the environment's project, other clusters there included: where that matters, use one
environment per cluster.

The cluster's status says which user it acts as (`status.credential`) and the condition `ClusterCredential`:
True / OwnCredential, or False with UserRefused, RoleRefused, PasswordRefused or CredentialRefused and the
identity service's message.

The installation sets the user's name, roles and credential name in the realization
(`openstack.clusterIdentity`: `userName`, `roles`, `rolesBySite`, `credentialName`; `{build}` is the build name),
and the sign-in client and the roles of access groups (`openstack.tenantSignIn`: `clientId` with `{site}`,
`editRoles`, `viewRoles`, `realm`).

## Environments of an earlier release

An environment made before this change had an application credential of the platform's account. At its next
reconcile the platform deletes that credential and its secret; the environment then holds no credential.
