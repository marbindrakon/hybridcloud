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

### Prerequisite: the issuer's length

The identity provider's remote ID is the issuer URL of the tenant realm, and an identity service can limit its
length: Keystone 2026.2 accepts at most **64 characters** and refuses a longer one when the provider is registered
(HTTP 400, "is too long"). The issuer is

```text
https://<host name of the sign-in directory>/realms/<tenant realm>
```

that is 8 + the host name + 8 + the realm name characters. With the realm `sovereign-tenants` (17 characters) the
host name may have at most 64 - 33 = 31 characters: `auth.cloud.example.com` (22, issuer 55 characters) fits; a
generated route name such as `keycloak-sso.apps.services.cloud.example.com` (44) does not. Give the sign-in
directory a short host name of its own before the first OpenStack site declares sign-in (the Keycloak chart's
`keycloak.routes`, with `keycloak.ingress.enabled: false` and `keycloak.hostname` set to the same name). The issuer
is part of every token and of every client's configuration, so changing it later moves every sign-in of the
platform.

When an identity provider that a site declares is not on the site and the tenant issuer is longer than 64
characters, the site's `SignInConfigured` message says so (IdentityProviderNotFound, with the issuer and its
length).

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

- **Tenant domain** (default, recommended with one tenant domain): every identity call of the platform stays
  inside the tenant domain, so the domain manager role on that domain is enough for the identity steps (projects,
  users, groups and their roles). It is not enough for the site's shared lists: see
  [Rights of the platform's account](#rights-of-the-platforms-account).
- **Federation domain** (the domain of the people who sign in): set `spec.openstack.signIn.groupDomain` to it.
  Each grant then crosses domains and needs the `admin` role: with a domain manager account the environments'
  access step says so (AccessReady False / GrantRefused, naming the domain), and the site probe cannot confirm the
  group domain. A group there is deleted only when the administrator's read of its roles on other projects
  succeeds; otherwise it is left on the site and the job says so.

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

The `OIDC-` prefix is the web server's claim prefix; use the one your web server sets. The first rule is meant to give
every person a user, also a person without any group on this site (expected from the mapping rules; what such a
person sees has not been tried on a site yet). A group named in the token that does not exist in
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
| The site's shared external networks (what tenants pick for a cluster) | `GET /v2.0/rbac-policies?action=access_as_external&target_project_id=*` | `admin` (see below) |
| The site's public volume types | `GET /v3/<project>/types` | `admin` on a cloud whose policy refuses others (see below) |

A domain manager is enough for the identity steps only. The site probe also reads two lists of the cloud that are
not in any domain: the shared external networks and the public volume types. For an account that is not an
administrator of the cloud, the network service shows no network policies but the account's own project's, and a
cloud may refuse the volume types (on a 2026.2 cloud with its default policies, the probe of a manager-only account
got none of the networks and "ForbiddenException" for the volume types). The probe then does not publish empty
lists: `status.site.externalNetworks` and `status.site.volumeTypes` keep what they held (nothing on a new site), and
the site's message says "external networks not listed: the account is not an administrator of the cloud, ..." (also
when such an account sees some networks: that list could be incomplete) and
"volume types not listed: the account may not read them ...". Tenants then have no external network to pick in the
cluster form. Give the account the right to read those lists (an administrator account, or a site policy that lets
it read them) where tenants should pick.

Facts that decide the design: a domain manager cannot delete another user's application credential (403), but can
delete the user, and the user's credentials go with it. A domain manager cannot read identity providers (403). The
site job gives the account `admin` on the tenant domain only when the account already holds `admin`; otherwise it
reports the roles it found.

## Cluster users

Each OpenShift cluster on an OpenStack environment acts as its own user of the environment's domain, named
after the cluster's build name by default, with `member` on the environment's project. The user:

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

The cluster's status says which user it acts as (`status.credential`, with `createdAt`: when the platform made the
credential, in UTC; the identity service does not report it) and the condition `ClusterCredential`:
True / OwnCredential, or False with UserRefused, RoleRefused, PasswordRefused or CredentialRefused and the
identity service's message. A site that does not answer (5xx, a timeout) refuses nothing: the step fails and the next
run tries again, with nothing changed.

The platform makes a new credential only when the cluster has none recorded, or when the site says the recorded one
is not valid. When the cluster recorded its credential but the platform's secret store no longer holds its secret
(the path deleted, the store restored to an earlier state), nothing is changed on the site, because the cluster may
still be using the credential: ClusterCredential False / CredentialSecretMissing. A platform administrator restores
the secret at `cluster/oso/<build>/build/provider-creds`, or replaces the credential in the cluster by hand.
Replacing the credential of a running cluster is not offered by the platform yet.

The installation sets the user's name, roles and credential name in the realization
(`openstack.clusterIdentity`: `userName`, `roles`, `rolesBySite`, `credentialName`; `{build}` is the build name,
which already carries the platform's prefix; the default for both names is `{build}`). The platform trusts the mark
in the user's description, never the name: a user of that name without it is refused, never changed or deleted.
The realization also sets the sign-in client and the roles of access groups (`openstack.tenantSignIn`: `clientId`
with `{site}`, `editRoles`, `viewRoles`, `realm`).

## Environments of an earlier release

An environment made before this change had an application credential of the platform's account. At its next
reconcile the platform deletes that credential and its secret; the environment then holds no credential.
