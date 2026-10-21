# Glossary

The words Hybrid Sovereign Cloud uses, what each one is in the API, and what it is closest to in
the clouds you may know. The table is generated from the content contract
([docs/content/terms.yaml](../content/terms.yaml)) and the API definitions; the sections after it
answer the questions people ask most.

## Terms

<!-- terms:begin -->
| Term | In the API (`oc get ...`) | Comparable to | Note |
|---|---|---|---|
| tenant | `Entity`: `entities`, `ent` | AWS: an AWS account under an organizational unit; Google Cloud: a Google Cloud project under a folder; Azure: an Azure subscription; OpenStack: an OpenStack domain | Not an Entra ID tenant. All tenants share one sign-in directory. |
| tenant profile | `TenantProfile` (available with this release) | - | Read-only. One object named shc in each tenant namespace, written by the platform: the tenant's display name, contacts and the sites it may use. |
| site | `CloudInfrastructure`: `cloudinfrastructures`, `cinfra` | AWS: an AWS Region; Google Cloud: a Google Cloud region; Azure: an Azure region; OpenStack: an OpenStack region | - |
| OpenStack environment | `CloudOSO`: `cloudosos` | AWS: an AWS account in one Region; Google Cloud: a Google Cloud project; OpenStack: an OpenStack project with its quota | - |
| OpenShift Virtualization environment | `CloudVirt`: `cloudvirts`, `cvirt` | AWS: an AWS account in one Region; Google Cloud: a Google Cloud project; OpenStack: an OpenStack project with its quota | Short form: VM environment. |
| AWS environment | `CloudAWS`: `cloudawss`, `caws` | AWS: an AWS account | - |
| Google Cloud environment | `CloudGCP`: `cloudgcps`, `cgcp` | Google Cloud: a Google Cloud project | - |
| network (hybrid network) | `HybridNetwork`: `hybridnetworks`, `hnet` | AWS: an Amazon VPC; Google Cloud: a VPC network; Azure: an Azure virtual network; OpenStack: a Neutron network with its router | - |
| network placement | `NetworkPlacement`: `networkplacements`, `nplace` | AWS: a VPC of its own, attached to the network; Google Cloud: a VPC of its own, attached to the network; Azure: a subnet; OpenStack: a subnet on the network's router | - |
| edge connection | `AttachmentCircuit`: `attachmentcircuits`, `acirc` | AWS: an AWS Direct Connect virtual interface; Google Cloud: a Cloud Interconnect VLAN attachment; Azure: an ExpressRoute private peering | - |
| access group | `Rbac`: `rbacs`, `rb` | AWS: an IAM Identity Center group; Google Cloud: a Google group; Azure: a Microsoft Entra group; OpenStack: a Keystone group | - |
| OpenShift cluster | `PlatformOpenshift`: `platformopenshifts` | AWS: Red Hat OpenShift Service on AWS; Google Cloud: Google Kubernetes Engine; Azure: Azure Red Hat OpenShift | - |
| team setup | `Team`: `teams` | OpenShift: an Argo CD AppProject (approximately) | Holds no people. Access groups hold people. |
| app project | `Project`: `projects`, `proj` | OpenShift: an OpenShift project on the tenant's cluster | Always qualified. Not an OpenStack project, not a Google Cloud project. |
| cluster assignment | `Assignment`: `assignments` | - | - |
| Vault | `Vault`: `vaults`, `vlt` | AWS: AWS Secrets Manager; Google Cloud: Secret Manager; Azure: Azure Key Vault; OpenStack: Barbican | - |
| KV secrets engine | `VaultKV`: `vaultkvs`, `vkv` | AWS: a set of AWS Secrets Manager secrets; Azure: an Azure Key Vault | - |
| AAP organization | `AAPOrg`: `aaporgs`, `aaporg` | - | - |
| Quay organization | `QuayOrg`: `quayorgs`, `qorg` | AWS: an Amazon ECR registry; Google Cloud: an Artifact Registry repository; Azure: an Azure Container Registry | - |
| VM migration to OpenStack | `OpenStackMigration`: `openstackmigrations`, `osm` | AWS: AWS Application Migration Service; Azure: Azure Migrate | Experimental. |
<!-- terms:end -->

`oc get` takes the plural or a short name: `oc get hybridnetworks -n entity-<tenant>`. The
comparisons are the closest equivalent, not an exact match.

## Tenant

Your organisation's space in the platform. It is **not** a Microsoft Entra ID tenant: all tenants
share one sign-in directory, and a tenant is a set of people, roles and objects inside it. Your
tenant's objects live in the namespace `entity-<tenant>` on the services cluster. The tenant's
display name, contacts and the sites it may use are in the **tenant profile** (the object `shc` in
that namespace, `oc get tenantprofile shc -n entity-<tenant> -o yaml`), available with this
release.

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

## What do I create for ...?

| You want | Create |
|---|---|
| A VPC or virtual network | a **network** |
| A subnet of that network in one account or project | a **network placement** (**Attach network** on the environment or network page) |
| An account, a subscription or a project on a cloud | an **environment** of that cloud's type (VM, OpenStack, AWS, Google Cloud) |
| A group of people | an **access group** (members are added in the sign-in directory) |
| A role binding for a group | give the access group a role on the tenant (your tenant admin or the platform team), or use the access fields of a team setup or app project |
| A private link to your own router | an **edge connection** |
| A secrets store | a **Vault** and, inside it, a **KV secrets engine** |

## States and Retry now

Every object shows one state: **Waiting** (for something else to be ready), **Creating**,
**Updating**, **Ready**, **Degraded** (usable with reduced health), **Failed** or **Deleting**, with a
one-line summary of why. [Status reasons](status-reasons.md) lists the reasons and who acts on
each one. **Retry now** asks the platform to process the object again at once, for example after
you fixed what a failure named.
