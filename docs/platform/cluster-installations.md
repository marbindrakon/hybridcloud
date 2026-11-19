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
(`status.install.namespaceWaitSince`), then it is refused (`NamespaceTaken`) for the platform team; a Retry checks
again. The OpenStack preflight also refuses an
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
| `removalLimitMinutes` | 60 | A removal of a stopped installation that waits this long beside a build Hive does not finish deleting is written as failed (`InstallRemovalFailed`). |
| `namespaceWaitMinutes` | 30 | A cluster whose build's namespace is still held by an earlier cluster of its name (terminating) waits this long, then is refused (`NamespaceTaken`). |

The number of installer attempts is the `ClusterDeployment`'s `installAttemptsLimit` (3, set by the
`mce-cluster-build` chart).

## The cause a tenant sees

The job reads the cause of a failed attempt from the installer's log on the `ClusterProvision` (`spec.installLog`) and
from Hive's failure condition, with fixed patterns, and writes a sentence of its own: quota shortfalls with the
resource and the numbers (`QuotaExceeded`), a flavor, instance type or image the installer refused, named only when it
is the value the tenant set (`InstallSettingRefused`), refused credentials (`InstallCredentialsRefused`), an attempt
that could not start or a release of the build that failed with no build on the hub (`InstallNotStarted`), anything
else, an attempt that left no log included (`InstallFailed`, with Hive's reason code when it is one of Hive's known
codes). The whole stored log is read (up to 4 MiB, more than an object can hold on the API server). No text of the
log, of Hive's messages or of the site reaches the cluster object.

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
