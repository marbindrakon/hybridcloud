# OpenShift cluster installations: watching, stops and retries

What the platform does while it installs an OpenShift cluster (OpenStack, AWS and Google Cloud environments), what a
tenant sees when an installation stops, what you can read on the hub, and what changes for clusters that exist when
this release is installed.

## What the provision job does

The provision job of a cluster makes the cluster's build on the hub (the Helm release of `mce-cluster-build`, which
creates Hive's `ClusterDeployment`) and then watches it:

- Every `observeIntervalSeconds` it reads the cluster object, the `ClusterDeployment` and the build's
  `ClusterProvision` objects. It writes `status.install` and the `Ready` condition when something changed, and the
  check time (`status.install.observedAt`) at least every `checkWriteMinutes`.
- It ends at once, without a write, when the cluster object is deleted, replaced by another object of the same name,
  or given a newer request (Retry now, a spec change). The same check runs right before the job makes the release;
  there it also compares the object's generation with the one the job read its spec at, so a job installs with the
  spec it read or not at all (a newer spec ends the job, which asks the operator for a new run at once).
- When Hive stops the installation (`ProvisionStopped`), the job writes the stop (`Stalled=True` and `Ready=False` with
  the cause's reason) and fails. The operator then publishes no event for the cluster until the tenant asks: a Retry
  request, or a change of `spec.type` or of the installation settings of the provider's block (compared by a digest of
  their values recorded at the stop, `status.install.stoppedSpec`, with the version of the list of settings it covers,
  `stoppedSpecVersion`: a digest of another version, as after a product update, is never compared; only a Retry asks
  then, and the next run renews it). Any other spec change is published once: the job applies what needs no installation
  (the viewer access), records the generation and ends without removing or installing anything. A Retry request that no
  job acted on, or an event published after the stop that no job completed, is published again.
- When the installation has not finished `deadlineMinutes` after it started (`InstallOverdue`), the job writes it as
  failed without `Stalled` and ends without failing. Each later periodic run of the cluster makes one observation: an
  installed build goes on to the usual steps and becomes Ready; a stop is written with its cause; otherwise the run
  ends.
- A ClusterDeployment that is being deleted (a removal for a new installation, or the cluster's delete) is never read
  as a stop: the watch concludes nothing from it.
- A run that ends with nothing to write (the hub could not be read at its last check, a removal under way that cannot
  go on now) completes its event, so the operator's next pass runs again; the status shown stays as it is.
- A job's start and acknowledgement writes keep what a running installation shows (its Ready reason and message, and
  the Failed phase of an installation past its deadline).

A recorded stop is checked against the hub at every run of the cluster; the hub wins:

| The hub holds | Nobody asked | A Retry, or the installation settings changed |
|---|---|---|
| no ClusterDeployment (a failed or deployed release, one pending for longer than `pendingReleaseMinutes`, or none) | the stop stays | the release is removed, a new installation starts |
| no ClusterDeployment, a release pending for less than `pendingReleaseMinutes` | the stop stays | the release is left alone; the cluster reads "The installation is starting." and the next run watches it |
| a stopped ClusterDeployment | the stop stays | the build is removed, a new installation starts |
| a ClusterDeployment that runs | the record takes the hub's state (without the stop's last failure); the watch goes on | the same; nothing is removed |
| an installed ClusterDeployment | the record takes the hub's state; Ready as for any installed cluster | the same |
| a ClusterDeployment being deleted | the stop stays | the stop stays and nothing of the record is renewed: the Retry or the changed settings still ask, and the operator's next pass runs again |
| nothing readable | the stop stays | the same |

A build the hub shows running or installed under a recorded stop is adopted as it is: settings changed while the
record said Stopped stay in the spec and are used only by a later installation, if any. While an installation runs
on the hub (a ClusterDeployment exists) the release is not changed, and the OpenStack preflight does not refuse that
build for a flavor it was not made with. An installation recorded as running whose ClusterDeployment does not exist
(its release failed, was left pending by a job that ended, or never made one) gets its release made again at the next
run, unless an older job is still making it (pending, younger than `pendingReleaseMinutes`; a release whose time
cannot be read counts as older); a release that keeps failing is written as a stop, and a release made again starts
the installation's time anew. The hub is read a few times; while it cannot be read nothing is released and the run
ends with its event completed (the next periodic run goes on). A removal under way waits while Hive deprovisions the
build (the event is completed; the run after it is gone continues), for at most `removalLimitMinutes` from the
removal's start; after that the cluster is written Failed (`InstallRemovalFailed`) for the platform team. The flavor
checks of the OpenStack preflight are left out only for a ClusterDeployment that runs or is installed (a stopped one a
Retry replaces is a new installation and is checked).

A Retry request made before the stop was written (while the page still said Installing) is recorded as handled by
the write that first records the stop (`status.install.stoppedAt` holds that time); only a request whose own time
(the request's value; the operator's publish time when the value is not a time) is later than the stop's write starts
a new installation, also when the operator handled it after the write. A run that meets a removal under way never
claims the build's namespace (the removal claims it once the old one is gone). A namespace that is still terminating
(an earlier cluster of the same name being removed) is never claimed: the cluster waits with reason
`WaitingForEarlierCluster` for at most `namespaceWaitMinutes` (30) from its first wait
(`status.install.namespaceWaitSince`), then it is refused (`EarlierClusterNotRemoved`) for the platform team; a Retry
checks again. The removal of a stopped installation waits for the build's hub namespace only while it is being
deleted, or when the removal deleted a ManagedCluster (the hub's cluster management may delete the namespace with it);
a build that stopped before it was connected has no ManagedCluster, nothing deletes its namespace, and the removal
claims it again at once. The namespace is the platform's own on every provider (the job makes it and marks it with
the cluster); the removal never deletes it. The OpenStack preflight also refuses an
image the environment does not offer (`ImageNotFound` when the spec names it, `DefaultImageNotFound` for the
installation's default). A cluster from before installations were recorded whose installation started and whose
ClusterDeployment is gone gets its release made again only on a Retry.

A cluster recorded as installed (`status.clusterInstalled`, or stage `Finishing` or `Installed`) whose
ClusterDeployment the hub does not hold (read from a list of the build's namespace that answers and is empty; a
hub that does not answer for the kind is unreadable, not missing) never gets a release or an installation, whoever
asks: a new installation
under its name could take the addresses and DNS names of a cluster that may still run on the site. Every run writes
it Failed with reason `BuildRecordMissing` (no `Stalled`; a Retry checks the same and does the same nothing) until the
platform team restores the build's record on the hub; only the object's deletion leads to a new installation. The test module `tests/unit/test_install_states.py` holds the table of every recorded
state against every hub state and kind of request.
- After `watchWindowMinutes` it writes that the installation is still running, completes its request and ends; the
  next periodic run of the cluster watches on.
- Only an installed cluster is written Ready.

The job template has a time limit of 150 minutes. In every stage a job works in (`Preparing`, `CleaningUp`,
`Installing`, `Finishing`) the operator treats a cluster's request as in flight only while the later of the job's
start and its last check time (`status.install.observedAt`) is younger than 15 minutes (variable
`in_flight_observed_stale_minutes` of the namespace operator). The job writes its check time when each long phase
starts and at least every five minutes in it (the watch, the removal's waits, the steps after the install), so a living
job is never replaced and one that died is replaced within about 25 minutes, also when it died before its first check.
In the other stages the job time applies.

### Settings

Installation defaults, in the `platformopenshift_defaults` inventory variable under `install`:

| Key | Default | Meaning |
|---|---|---|
| `observeIntervalSeconds` | 30 | How often the job looks at the installation. |
| `watchWindowMinutes` | 120 | How long one job watches; keep it below the job template's time limit. |
| `deadlineMinutes` | 360 | An installation neither finished nor stopped after this long is shown failed (`InstallOverdue`) and checked once per periodic run. |
| `checkWriteMinutes` | 5 | How often the check time is written while nothing changes; keep it well below 15. |
| `pendingReleaseMinutes` | 10 | A pending release of a build with no ClusterDeployment younger than this is left alone by a Retry (an older job is still making it). |
| `retryMarginMinutes` | 1 | A Retry on a stopped installation starts a new one when its own time is after the stop was written, or when the operator published it more than this long after that write (a client clock that runs slow); a Retry published within it was in flight when the stop was written and starts nothing. |
| `removalLimitMinutes` | 60 | A removal of a stopped installation that waits this long beside a build Hive does not finish deleting is written as failed (`InstallRemovalFailed`). |
| `namespaceWaitMinutes` | 30 | A cluster whose build's namespace is still held by an earlier cluster of its name (terminating) waits this long (`WaitingForEarlierCluster`), then is refused (`EarlierClusterNotRemoved`). |

The number of installer attempts is the `ClusterDeployment`'s `installAttemptsLimit` (3, set by the
`mce-cluster-build` chart).

## Who gets what on a tenant's cluster

The identity realm is shared by all tenants. A tenant's cluster takes only its own tenant's groups:

- Each access group the cluster names (`spec.toolRbac`: `clusterAdminRbac`, `clusterOperatorRbac`,
  `clusterDeveloperRbac`, `clusterViewerRbac`) becomes a client role `<tenant>.<group>` of the cluster's own sign-in
  client, mapped to the access group's group in the directory, as on the services cluster and the sites.
- The client's groups claim comes from one place only: a mapper of the client's own roles. No other mapper of the
  client writes `groups`, and a client scope linked to the client whose mappers write `groups` (for example a
  realm's `microprofile-jwt` scope) is unlinked from this client, so asking for more scopes at sign-in adds nothing.
  The cluster sees the group `<tenant>.<group>` and never another tenant's group of the same name.
- The cluster's bindings (`<build>-cluster-admin`, `-cluster-operator`, `-cluster-developer`, `-cluster-viewer`)
  name exactly those groups; a level without an access group has no binding.
- Creating projects: OpenShift's default binding `self-provisioners` lets every signed-in person create projects. On
  a tenant's cluster the platform manages it: it names the operator level's groups only (none when that level has no
  access group), and carries `rbac.authorization.kubernetes.io/autoupdate: "false"` so that OpenShift does not put
  the default back. Administrators create projects through `cluster-admin`.
- A new project belongs to the cluster's groups, not to the person who asked for it. OpenShift's default project
  template makes the requesting user the project's admin; on a tenant's cluster the platform sets the cluster's own
  project template (`project-request` in `openshift-config`, named by the cluster's project configuration), whose
  admin binding names the operator level's groups. A person taken out of such a group, or moved to another tenant,
  loses the admin right of those projects when their token ends (below), including the projects they asked for. A
  cluster whose operator level has no access group: nobody but its administrators can create a project, and a new
  project is bound to no group; the cluster's message for a refused project request says so.
- A limit of the project template: OpenShift applies it once, when the project is made, and the platform does not
  change a project's admin binding afterwards. The binding names the groups that were the operator level's groups
  when the project was made. When a group leaves the operator level (moved to another level, or taken off the
  cluster), or is taken off and an access group of the same name is added to the cluster again later, the projects
  made before keep that group's name in their admin binding: the members of a group of that name on the cluster are
  admins of those projects. A group added to the operator level later is not admin of the projects made before. The
  cluster's administrators change those bindings where that is not wanted.
- An access group whose group is not in the directory yet gets nothing; the job's log names it.

**Who can sign in.** A person of the same sign-in directory who is not in one of the cluster's access groups can
authenticate at the cluster and then has no rights: no group, no binding, no project. They keep what OpenShift gives
every authenticated user, such as the `basic-user` role (basic information about their own user and the projects
they can see) and their own OAuth tokens, which they can list and delete. What would stop the authentication itself
is a sign-in directory (realm) per tenant.

**When a person leaves an access group.** OpenShift takes a person's groups from the sign-in directory at each
sign-in. The platform binds nothing to a person, only to groups, so a person taken out of an access group, or moved
to another tenant, keeps the cluster's group, and its rights, until their OAuth access token ends, and nothing after:
the cluster's token lifetime is the realization's `tenantCluster.oauthAccessTokenMaxAgeSeconds` (default 86400
seconds, OpenShift's own). A cluster administrator can end it sooner by deleting the person's tokens. An access group
taken off the cluster loses the cluster's bindings (its level's binding and `self-provisioners`) with the next run of
the cluster's job; the admin bindings of projects keep its name (the limit above). A binding that someone on the
cluster gave a person by name (a project administrator can), and a service account token they made, are the
cluster's administrators' to remove; the platform does not look for them.

The cluster's job needs no right in the identity provider beyond the realm's `clients` and `membership` automation
clients that the RbacConfig publishes: it manages the cluster's own client, its roles and their mappings to the
access groups' groups. The sign-in client carries the attribute `platformopenshift=<namespace>/<cluster>` and is
recorded in the cluster's `status.oidcClientId` as soon as it exists; a client of the same name marked for another
cluster, or without a mark when the cluster's `status.oidcClientId` does not record it, is refused and never changed
(the recorded build name is not proof: the cluster records it itself before its sign-in step). A run that skips the
sign-in step (the cluster's console is not known yet) leaves the recorded client as it is. The delete removes the recorded client only (when it carries the cluster's mark or none); a
cluster that recorded no client deletes nothing in the sign-in directory and says so.

**What the sign-in directory must allow.** The jobs manage a cluster's sign-in client with the realm's own account
for clients, which may manage clients but not the realm. The directory lets such an account add or change only the
protocol mapper types its realm lists (realm attribute `allowedProtocolMapperTypesForClientAdministrators`; with
none listed every mapper is refused). The job of the directory's connection (`RbacConfig`) lists the two types
the platform uses and keeps any an administrator added: group membership, for the tools' sign-in clients, and
client roles, for a cluster's and a site's sign-in client. After an upgrade of the directory that brings this
rule, run that connection's job once (Retry now on the `RbacConfig`) before a cluster or a secrets store is made;
until then their sign-in step stops with the directory's refusal.

## The names the platform gives a cluster

- **Build name** (`status.buildName`, the cluster's identity on the hub): `shc-<oso|aws|gcp>-<cluster>-<tenant>-<8
  hex>`, the cluster's own name first, cut to fit (53 characters on OpenStack, 40 on AWS and Google Cloud) and ended
  by a digest of the exact tenant and cluster. A recorded build name stays the cluster's.
- **OpenStack user and application credential**: `<tenant>_<environment>_<cluster>`, described with the same names.
  A cluster that recorded its user (`status.credential.user`) keeps that name.
- **OpenStack floating IPs**: described `OpenShift cluster <cluster>: API (platform build <build>)` and `...:
  ingress (...)` for people, and recorded with their addresses and ids in `status.floatingIPs`. A cluster that
  recorded its floating IPs keeps those two; with nothing recorded the job makes two and records them at once. They
  are never found by a description, and released by their recorded ids only. A run that made floating IPs and
  stopped before it recorded them leaves them to the platform team: the cluster's next run makes new ones, and the
  ones left behind are released by hand in the environment's project. The same holds when the second of the two
  cannot be made after the first was: the first is left unrecorded, and the job's log does not give its id; its
  description names the cluster. A record that holds only part of the pair
  stops the job and makes nothing: the platform team completes or clears `status.floatingIPs`.
- **Tags and labels** on what the installer makes on AWS and Google Cloud: `shc-tenant`, `shc-environment` and
  `shc-cluster` carry the names in full, next to `shc-managed` and `shc-build`.
- The cluster's, the tenant's and the environment's names are checked before anything is made: a letter first,
  lower-case letters, digits and single hyphens, a letter or digit last, at most 30 characters; on Google Cloud a
  cluster name may not start with `goog` or contain `google` or a look-alike (the installer's own rule). A name that
  breaks the rule stops the job with reason `NameNotAllowed`.

## The cause a tenant sees

The job reads the cause of a failed attempt from the installer's log on the `ClusterProvision` (`spec.installLog`) and
from Hive's failure condition, with fixed patterns, and writes a sentence of its own: quota shortfalls with the
resource and the numbers (`QuotaExceeded`), a flavor, instance type or image the installer refused, named only when it
is the value the tenant set (`InstallSettingRefused`), refused credentials (`InstallCredentialsRefused`), an attempt
that could not start or a release of the build that failed with no build on the hub (`InstallNotStarted`), anything
else, an attempt that left no log included (`InstallFailed`, with Hive's reason code when it is one of Hive's known
codes). The whole stored log is read (up to 4 MiB, more than an object can hold on the API server). No text of the
log, of Hive's messages or of the site reaches the cluster object.

The same holds for every other failure of the cluster's jobs (provision and delete). A call to a cloud, a site, the
hub or a helper that fails ends the job with the platform's sentence for the step, for example `The step "Prepare
the cluster's identity on Google Cloud" failed; the platform team can see why in the job's log.`; what was answered
(an API's error, an address, an id) is written to the job's log only. A refusal is written with its own reason and
its own sentence, the same in `status.message` and in its condition: the sentence is built from fixed text and values
the tenant set or chooses by name (the cluster's name, a network, flavor or image name, its own ranges and floating
addresses), never a site's or the hub's ids, uids, addresses or object names (those go to the job's log). A failure
after a refusal was noted (a status write, a module) is not the refusal. Any other failed step is named the same way
as a call when its name is plain words, else `A step of the cluster's provisioning failed; ...` (`deletion` for the
delete), with reason `ProvisionFailed` (`TeardownFailed` for the delete): the platform team reads the job's log named
in `status.edaJobs`. The same holds for the conditions the steps write (the router gateway's, the Preflight
condition) and for the cluster's events. AWS and Google Cloud clusters need the realization's `clusterIssuer.baseURL`
and `clusterIssuer.namespace`; there is no default, and the job names the missing setting. A delete never needs it:
it finds the cluster's static issuer (`oidc-static-<build>`) by that exact name and the build's label in any namespace,
and says so in its result when there is none.

**Upgrade note.** Before upgrading, set `clusterIssuer.baseURL` (an https address) and `clusterIssuer.namespace` in
`AWS_REALIZATION` and `GCP_REALIZATION` to the values in use; there is no default any more. Without them every AWS and
Google Cloud cluster job fails naming the setting.

The names of an Internal AWS or Google Cloud cluster (`api.` and `*.apps.` in the environment's zone) point at the
cluster's own internal load balancers, found by the installer's infrastructure id of the cluster: read from the
cluster's private zone (named after the cluster), then the load balancers tagged with exactly that id (AWS), or the
API rule named after it and the ingress rule whose instances are all the cluster's (Google Cloud). Another cluster on
the same network is never taken, whatever its name. When the cluster's own resources are not found yet, nothing is
published, the job's result and the cluster's status message say that the names are not published yet, and the next
periodic run tries again (a run waits at most about three minutes for them).

On AWS the public subnets, route table and internet gateway of a placement serve all External clusters on it: a
delete leaves them while another cluster object of the namespace records the same placement as External; the last
one's delete removes them. They are made in, and removed from, the VPC the placement records
(`status.realization.site.vpc`), never a VPC found by its tags. A placement that records no VPC stops the step and
nothing is made or deleted; a placement that no longer exists took its VPC with it.

On an OpenStack site with a router advertisement service, the cluster's preflight gives the service the platform's
project by the id the placement recorded (else the one the site's gateway recorded); while neither has recorded it,
the cluster waits (`WaitingForPlacement`).

On OpenStack the preflight refuses a flavor the environment does not offer before anything is created: named to the
tenant (`FlavorNotFound`) when the cluster's spec names it; when it is the installation's default
(`platformopenshift_defaults` keys `controlPlaneFlavor`, `workerFlavor`), the reason is `DefaultFlavorNotFound` and the
message names no flavor. These two defaults have no fallback: where an installation is about to start, a cluster
that needs one that is not set (the worker flavor only with workers) fails with the setting's name; an installed or
installing cluster never does.

To read the whole log of a stopped installation, use the build name from the cluster's `status.buildName`:

```bash
oc get clusterprovisions -n <build name>
oc get clusterprovision <provision> -n <build name> -o jsonpath='{.spec.installLog}'
```

## Retry installation

A stopped installation is removed and installed again under the same name only when all of these hold, read fresh by
the job: the hub's build is not installed, Hive has stopped it (or no `ClusterDeployment` is left), the cluster object
records no installed cluster, and the tenant asked after the stop (a new Retry request, or a change of `spec.type` or
the provider's block). The job records `CleaningUp` (the next `run`), reads the hub's build once more, deletes the
`ManagedCluster` and the `ClusterDeployment` (Hive deprovisions what the last attempt built), uninstalls the release
and installs again. Every wait of the removal reads the cluster object too and ends the job when it is deleted,
replaced or given a newer request; a later job that finds `CleaningUp` continues the removal through the same checks.
The cluster keeps its floating IPs, DNS records, credential and secret store paths. A failed attempt Hive is still
retrying is never removed; nothing reinstalls by itself. A release of the build is tried twice; one that still fails
while no `ClusterDeployment` exists is written as a stop (`InstallNotStarted`), so the tenant can change the settings
and retry.

The API refuses a change of a cluster's installation settings (node counts, flavors or instance types, release, image
and the rest the installer reads once) while the installation is cleaning up, installing or finishing, and once the
cluster is installed. The worker count on AWS and Google Cloud is refused only while the cluster is being installed:
on an installed cluster every run renders the worker pool (Hive `MachinePool`, replicas from the worker count) and the
hub scales it. The worker instance or machine type stays an installation setting: Hive refuses a change of a pool's
platform once the pool exists. `spec.type`, the environment, the region and the network (AWS, Google Cloud and
OpenStack) cannot change after creation.

## What changes for existing clusters

- **Installed clusters:** nothing. They get `status.install.stage: Installed` at their next periodic run (with no
  `endedAt`: the end of an installation before this release is not known). Their installation settings can no longer
  be changed: on OpenStack the platform never applied them to an installed cluster; on AWS and Google Cloud only the
  worker count was applied, and it still is.
- **All clusters:** `spec.type` and the environment can no longer change, and on AWS and Google Cloud neither can the
  region nor the network.
- **Sign-in of existing clusters:** the next run of an installed cluster gives it the tenant's groups as above: its
  bindings are replaced (the groups without the tenant are no longer bound), its sign-in client is marked as the
  cluster's, `self-provisioners` names its operator groups only, and new projects are bound to the operator groups.
  Projects made before keep their admin binding to the person who asked for them: the cluster's administrators
  review them (`oc get rolebindings -A`) and remove the bindings to people by name. People who sign in again get
  their groups `<tenant>.<group>`; the cluster's old groups of bare names are left on the cluster with no binding and
  can be deleted there.
- **AWS and Google Cloud issuers:** the static issuer's ConfigMap of an existing cluster carries the label keys of the
  release before; the issuer's sync selects both keys in this release, and the cluster's next run writes both.
- **OpenStack clusters of the release before** left the install-config secret at
  `oso/projects/<environment>/clusters/<cluster>/install-config` in the secret store; a delete no longer removes that
  path (the cluster's name alone is not unique across tenants). The platform team removes it by hand after the
  cluster is deleted.
- **Clusters installing during the upgrade:** a provision job that is already running finishes the old way; the next
  run of the cluster writes the new fields.
- **OpenStack clusters on a hybrid network whose installation stopped** are no longer reinstalled about every two
  hours: they stay Failed until the tenant retries.
- **AWS and Google Cloud clusters whose installation stopped** are no longer polled again every ten minutes; Retry
  installation now removes the stopped build and installs again (before, it only polled the same stopped build).
- **Clusters written Ready without an installation.** Before this release, an OpenStack cluster on a site's external
  network whose installation did not finish within two hours was written Ready. Find such clusters:

  ```bash
  oc get platformopenshifts -A -o json | jq -r '.items[]
    | select(.status.ready == true and (.status.clusterInstalled | not))
    | "\(.metadata.namespace)/\(.metadata.name) build \(.status.buildName)"'
  ```

  For each, use Retry now (`ansible.sdk.operatorframework.io/reconcileNow` set to the current time): the run reports
  the real state of the installation (installed, still running, or stopped with its cause).
