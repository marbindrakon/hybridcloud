# Glossary

The words Hybrid Sovereign Cloud uses, what each one is in the API, and what it is closest to in
the clouds you may know. The table is generated from the content contract
([docs/content/terms.yaml](../content/terms.yaml)) and the API definitions; the sections after it
answer the questions people ask most.

## Terms

<!-- terms:begin -->
| Term | In the API (`oc get ...`) | Comparable to | Note |
|---|---|---|---|
| tenant | `Entity`: `entities`, `ent`, `tenant` | AWS: an AWS account under an organizational unit; Google Cloud: a Google Cloud project under a folder; Azure: an Azure subscription; OpenStack: an OpenStack domain | Not an Entra ID tenant. All tenants share one sign-in directory. |
| tenant profile | `TenantProfile`: `tenantprofiles`, `tprofile` | - | Read-only. One object named shc in each tenant namespace, written by the platform: the tenant's display name, contacts, the sites it may use (with an OpenStack site's external networks) and the address ranges the platform reserves. |
| site | `CloudInfrastructure`: `cloudinfrastructures`, `cinfra`, `site` | AWS: an AWS Region; Google Cloud: a Google Cloud region; Azure: an Azure region; OpenStack: an OpenStack region | - |
| OpenStack environment | `CloudOSO`: `cloudosos`, `osenv` | AWS: an AWS account in one Region; Google Cloud: a Google Cloud project; OpenStack: an OpenStack project with its quota | - |
| OpenShift Virtualization environment | `CloudVirt`: `cloudvirts`, `cvirt`, `vmenv` | AWS: an AWS account in one Region; Google Cloud: a Google Cloud project; OpenStack: an OpenStack project with its quota | Short form: VM environment. |
| AWS environment | `CloudAWS`: `cloudawss`, `caws`, `awsenv` | AWS: an AWS account | - |
| Google Cloud environment | `CloudGCP`: `cloudgcps`, `cgcp`, `gcpenv` | Google Cloud: a Google Cloud project | - |
| network (hybrid network) | `HybridNetwork`: `hybridnetworks`, `hnet` | AWS: an Amazon VPC; Google Cloud: a VPC network; Azure: an Azure virtual network; OpenStack: a Neutron network with its router | - |
| network placement | `NetworkPlacement`: `networkplacements`, `nplace` | AWS: a VPC of its own, attached to the network; Google Cloud: a VPC of its own, attached to the network; Azure: a subnet; OpenStack: a subnet on the network's router | - |
| edge connection | `AttachmentCircuit`: `attachmentcircuits`, `acirc` | AWS: an AWS Direct Connect virtual interface; Google Cloud: a Cloud Interconnect VLAN attachment; Azure: an ExpressRoute private peering | - |
| access group | `Rbac`: `rbacs`, `rb`, `accessgroup`, `ag` | AWS: an IAM Identity Center group; Google Cloud: a Google group; Azure: a Microsoft Entra group; OpenStack: a Keystone group | - |
| OpenShift cluster | `PlatformOpenshift`: `platformopenshifts`, `ocpcluster` | AWS: Red Hat OpenShift Service on AWS; Google Cloud: Google Kubernetes Engine; Azure: Azure Red Hat OpenShift | - |
| team setup | `Team`: `teams` | OpenShift: an Argo CD AppProject (approximately) | Holds no people. Access groups hold people. |
| app project | `Project`: `projects`, `proj` | OpenShift: an OpenShift project on the tenant's cluster | Always qualified. Not an OpenStack project, not a Google Cloud project. |
| cluster assignment | `Assignment`: `assignments`, `clusterassignment` | - | - |
| Vault | `Vault`: `vaults`, `vlt` | AWS: AWS Secrets Manager; Google Cloud: Secret Manager; Azure: Azure Key Vault; OpenStack: Barbican | - |
| KV secrets engine | `VaultKV`: `vaultkvs`, `vkv` | AWS: a set of AWS Secrets Manager secrets; Azure: an Azure Key Vault | - |
| AAP organization | `AAPOrg`: `aaporgs`, `aaporg` | - | - |
| Quay organization | `QuayOrg`: `quayorgs`, `qorg` | AWS: an Amazon ECR registry; Google Cloud: an Artifact Registry repository; Azure: an Azure Container Registry | - |
| VM migration to OpenStack | `OpenStackMigration`: `openstackmigrations`, `osm` | AWS: AWS Application Migration Service; Azure: Azure Migrate | Experimental. |
<!-- terms:end -->

`oc get` takes the plural or a short name: `oc get hybridnetworks -n entity-<tenant>`. The
comparisons are the closest equivalent, not an exact match.

## Listing your objects

A category lists several kinds at once: `oc get shc-tenant -n entity-<tenant>` lists every
tenant object. For each kind in a category that your role may not list, `oc` prints a
`Forbidden` line instead, so a role that reads only some kinds uses a narrower category or names
the kinds. The categories, and the command for each role:

<!-- list-commands:begin -->
| Category | Kinds |
|---|---|
| `shc-tenant` | AAP organizations (`AAPOrg`), cluster assignments (`Assignment`), edge connections (`AttachmentCircuit`), AWS environments (`CloudAWS`), Google Cloud environments (`CloudGCP`), OpenStack environments (`CloudOSO`), OpenShift Virtualization environments (`CloudVirt`), networks (`HybridNetwork`), network placements (`NetworkPlacement`), VM migrations to OpenStack (`OpenStackMigration`), OpenShift clusters (`PlatformOpenshift`), app projects (`Project`), Quay organizations (`QuayOrg`), access groups (`Rbac`), team setups (`Team`), tenant profiles (`TenantProfile`), Vaults (`Vault`), KV secrets engines (`VaultKV`) |
| `shc-network` | edge connections (`AttachmentCircuit`), networks (`HybridNetwork`), network placements (`NetworkPlacement`) |
| `shc-environment` | AWS environments (`CloudAWS`), Google Cloud environments (`CloudGCP`), OpenStack environments (`CloudOSO`), OpenShift Virtualization environments (`CloudVirt`) |
| `shc-appplatform` | cluster assignments (`Assignment`), OpenShift clusters (`PlatformOpenshift`), app projects (`Project`), team setups (`Team`) |
| `shc-services` | AAP organizations (`AAPOrg`), Quay organizations (`QuayOrg`), Vaults (`Vault`), KV secrets engines (`VaultKV`) |
| `shc-access` | access groups (`Rbac`) |

| Role | Lists what the role reads |
|---|---|
| Tenant admin | `oc get shc-tenant -n entity-<tenant>` |
| Auditor | `oc get shc-tenant -n entity-<tenant>` |
| OpenStack environment admin | `oc get shc-access,cloudosos -n entity-<tenant>` |
| OpenStack environment viewer | `oc get shc-access,cloudosos -n entity-<tenant>` |
| VM environment admin | `oc get shc-access,cloudvirts -n entity-<tenant>` |
| VM environment viewer | `oc get shc-access,cloudvirts -n entity-<tenant>` |
| Network admin | `oc get shc-network,shc-environment,shc-access -n entity-<tenant>` |
| Network viewer | `oc get shc-network,shc-access -n entity-<tenant>` |
| AWS environment admin | `oc get shc-access,cloudawss -n entity-<tenant>` |
| AWS environment viewer | `oc get shc-access,cloudawss -n entity-<tenant>` |
| OpenShift cluster admin | `oc get shc-access,platformopenshifts,cloudosos,cloudawss,cloudgcps -n entity-<tenant>` |
| OpenShift cluster viewer | `oc get shc-access,platformopenshifts -n entity-<tenant>` |
| Team setup admin | `oc get shc-access,teams,assignments -n entity-<tenant>` |
| Team setup viewer | `oc get shc-access,teams,assignments -n entity-<tenant>` |
| App project admin | `oc get shc-access,projects.hybridsovereign.redhat,assignments -n entity-<tenant>` |
| App project viewer | `oc get shc-access,projects.hybridsovereign.redhat,assignments -n entity-<tenant>` |
| Cluster assignment admin | `oc get shc-access,assignments,projects.hybridsovereign.redhat,teams -n entity-<tenant>` |
| Access group admin | `oc get shc-access -n entity-<tenant>` |
<!-- list-commands:end -->

Every role also reads the tenant profile (`oc get tenantprofile shc -n entity-<tenant>`).
Objects shared with you through an access field (team setups, app projects) are not in a list;
get them by name ([access and delegation](access-and-delegation.md)).

## Tenant

Your organisation's space in the platform. It is **not** a Microsoft Entra ID tenant: all tenants
share one sign-in directory, and a tenant is a set of people, roles and objects inside it. Your
tenant's objects live in the namespace `entity-<tenant>` on the services cluster. The tenant's
display name, contacts and the sites it may use are in the **tenant profile** (the object `shc` in
that namespace, `oc get tenantprofile shc -n entity-<tenant> -o yaml`), available with this
release.

## Names

**The rule for names.** Every name you choose for an object that reaches a cloud or a site (your
tenant, an environment, a network, a network placement, an OpenShift cluster, an access group, a
team setup, an app project, a cluster assignment, a Vault, a KV secrets engine, a Quay or AAP
organization, an edge connection) follows one rule:

- a letter first, then lower-case letters, digits and single hyphens (`-`), a letter or a digit
  last; no `--`, no dots, no underscores;
- at most **30** characters.

`example-corp`, `prod`, `web-2` are allowed; `2web`, `web.prod`, `web--prod`, `Web` and a name of
31 characters are refused. The API refuses such a name when you create the object, with the rule in
the message (`metadata.name: Invalid value: ...`); the dashboard's forms check it as you type. A name
cannot change later: to rename, create a new object and delete the old one. An object made before
the rule keeps its name; its next run stops with the reason `NameNotAllowed` before it makes
anything ([status reasons](status-reasons.md)).

Two more rules:

- On Google Cloud, an OpenShift cluster's name may not start with `goog` or contain `google` or a
  word like it (`g00gle`): Google Cloud refuses such names.
- An environment's name is unique within your tenant across the four environment types: an
  OpenStack environment and a VM environment of your tenant cannot both be called `prod`
  (`EnvironmentNameTaken`). Another tenant may use the same name.

**What you see on each platform.** The platform names what it makes from your own names, in one
order (tenant, then environment or network placement, then the object), joined by a character your
names cannot contain: `_` where the platform allows it, `--` where only letters, digits and hyphens
are allowed. Access groups keep `<tenant>.<group>`. Nothing random is added. For the tenant
`example-corp`, the environment `prod`, the network placement `core-prod`, the cluster `web` and the
access group `devs`:

| What | Where you see it | Its name |
|---|---|---|
| The OpenStack project of an OpenStack environment | the OpenStack site (dashboard, CLI) | `example-corp_prod` (`status.projectName`) |
| The network and router of a network placement on an OpenStack site | the OpenStack project | `example-corp_core-prod`; subnets `example-corp_core-prod_v4`, `example-corp_core-prod_v6` (`status.siteNetwork.name`) |
| An access group on an OpenStack site, on a VM site, on the services cluster and on your OpenShift clusters | group lists, role bindings | `example-corp.devs` |
| The same access group in the sign-in directory | the sign-in directory, the token's groups | `example-corp/devs` |
| A VM environment's namespace on the OpenShift Virtualization site | the site's console | `example-corp--prod--<namespace>` |
| An OpenShift cluster's user on an OpenStack site | the OpenStack project's users | `example-corp_prod_web` |
| A Quay or AAP organization | Quay, the automation platform | `example-corp_<organization>` (`status.orgName`) |
| A Vault's sign-in client | the sign-in directory | `vault_example-corp_<vault>` (`status.oidcClientId`) |
| A network placement on AWS | the `Name` tag in your AWS account | `example-corp_core-prod` |
| A network placement on Google Cloud | your Google Cloud project | `shc-example-corp--core-prod` |
| DNS | DNS | `<environment>.<tenant>.<tenant DNS domain>`, a cluster's API `api.web.prod.example-corp.<tenant DNS domain>` |

Where a platform's limit is shorter than the whole name (a Google Cloud network placement's objects,
63 characters), the name is cut and ends in six characters derived from your exact names, so that
two different names never meet; the full names are in the object's description, labels or tags.
What the OpenShift installer names inside your project or account (`web-<five characters>-master-0`)
is the installer's own form.

## Which project is which

Four different things are called "project". In this documentation the word is always qualified.

| You read | What it is | Where it lives | How to list it |
|---|---|---|---|
| **App project** | A set of application namespaces for a team on your tenant's OpenShift clusters; a cluster assignment links it to clusters (API kind `Project`) | Services cluster, your tenant namespace | `oc get projects.hybridsovereign.redhat -n entity-<tenant>` (short name `proj`) |
| **OpenStack project** | The project behind an OpenStack environment: quota, networks and instances on an OpenStack site | The OpenStack site | `status.projectName` of the OpenStack environment; the site's own tools |
| **Google Cloud project** | The project behind a Google Cloud environment | Google Cloud | `status.project` of the Google Cloud environment; the Google Cloud console |
| **OpenShift project** | A namespace on any OpenShift cluster, as `oc projects` shows it | Every cluster, including the services cluster | `oc get projects` (this lists namespaces, not app projects) |

`oc get projects` on the services cluster therefore shows your tenant namespace `entity-<tenant>`,
not your app projects. Use the full name `projects.hybridsovereign.redhat` or `proj` for app
projects.

## Team setup, access group, role

- An **access group** holds people. Members are added in the sign-in directory, as the group
  `<tenant>/<access group>`.
- A **role** says what an access group may do in the tenant, for example network admin or VM
  environment admin. Roles are given to access groups on the tenant.
- A **team setup** holds no people. It names the app projects and cluster assignments a team works
  with, and (in its access fields) which access groups may see or change them.

## Words for clusters on networks

- **Router attachments**: a network placement on an OpenStack environment with **Use for router
  attachments** on lets the environment's routers use the network as their external network.
  Comparable to an OpenStack external network, but for one project only.
- **Floating IP**: an OpenStack address a router translates to a server or a load balancer. An
  OpenShift cluster on OpenStack has two (API and applications), on the network it is published on.
- **Platform access**: the platform's management may reach the OpenShift clusters it builds on a
  network, and those clusters the platform's service endpoints. On by default; the network's
  `PlatformReachable` condition says whether the platform reaches it.
- **Reserved ranges**: address ranges the platform keeps for itself. Your networks cannot use the
  refused ones, and the platform allocates no subnets from the others. Listed in the tenant profile
  and on the network pages ([OpenShift clusters on your networks](clusters-on-networks.md)).

## What do I create for ...?

| You want | Create |
|---|---|
| A VPC or virtual network | a **network** |
| A subnet of that network in one account or project | a **network placement** (**Attach network** on the environment or network page) |
| An account, a subscription or a project on a cloud | an **environment** of that cloud's type (VM, OpenStack, AWS, Google Cloud) |
| A group of people | an **access group** (members are added in the sign-in directory) |
| A role binding for a group | give the access group a role on the tenant (your tenant admin or the platform team), or use the access fields of a team setup or app project |
| A private link to your own router | an **edge connection** |
| An OpenShift cluster on one of your networks | an **OpenShift cluster** that names the network ([OpenShift clusters on your networks](clusters-on-networks.md)) |
| A secrets store | a **Vault** and, inside it, a **KV secrets engine** |

## States and Retry now

Every object shows one state: **Waiting** (for something else to be ready), **Creating**,
**Updating**, **Ready**, **Degraded** (usable with reduced health), **Failed** or **Deleting**, with a
one-line summary of why. [Status reasons](status-reasons.md) lists the reasons and who acts on
each one. **Retry now** asks the platform to process the object again at once, for example after
you fixed what a failure named.
