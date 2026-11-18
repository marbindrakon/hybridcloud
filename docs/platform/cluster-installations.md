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
  request, or a change of `spec.type` or the provider's block (compared by a digest recorded at the stop,
  `status.install.stoppedSpec`). Any other spec change is published once: the job applies what needs no installation
  (the viewer access), records the generation and ends without removing or installing anything. A Retry request that no
  job acted on, or an event published after the stop that no job completed, is published again.
- When the installation has not finished `deadlineMinutes` after it started (`InstallOverdue`), the job writes it as
  failed without `Stalled` and ends without failing. Each later periodic run of the cluster makes one observation: an
  installed build goes on to the usual steps and becomes Ready; a stop is written with its cause; otherwise the run
  ends.
- A ClusterDeployment that is being deleted (a removal for a new installation, or the cluster's delete) is never read
  as a stop: the watch concludes nothing from it.
- After `watchWindowMinutes` it writes that the installation is still running, completes its request and ends; the
  next periodic run of the cluster watches on.
- Only an installed cluster is written Ready.

The job template has a time limit of 150 minutes. While the stage is `Installing`, the operator treats a cluster's
request as in flight while the job's last check time is younger than 15 minutes (variable
`in_flight_observed_stale_minutes` of the namespace operator), so a job that died while it watched is replaced within
about 25 minutes. In the other stages (a removal of a stopped installation, the steps after the install) the job time
applies.

### Settings

Installation defaults, in the `platformopenshift_defaults` inventory variable under `install`:

| Key | Default | Meaning |
|---|---|---|
| `observeIntervalSeconds` | 30 | How often the job looks at the installation. |
| `watchWindowMinutes` | 120 | How long one job watches; keep it below the job template's time limit. |
| `deadlineMinutes` | 360 | An installation neither finished nor stopped after this long is shown failed (`InstallOverdue`) and checked once per periodic run. |
| `checkWriteMinutes` | 5 | How often the check time is written while nothing changes; keep it well below 15. |

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
message names no flavor. These two defaults have no fallback: a cluster that needs one that is not set fails with the
setting's name.

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
retrying is never removed; nothing reinstalls by itself. A release of the build that fails while no `ClusterDeployment`
exists is written as a stop (`InstallNotStarted`), so the tenant can change the settings and retry.

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
