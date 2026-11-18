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
  or given a newer request (Retry now, a spec change). The same check runs right before the job makes the release.
- When Hive stops the installation (`ProvisionStopped`), or the installation has not finished `deadlineMinutes` after
  it started, the job writes the stop (`Stalled=True` and `Ready=False` with the cause's reason) and fails. The
  operator then publishes no event for the cluster until the tenant asks (Retry installation or a spec change).
- After `watchWindowMinutes` it writes that the installation is still running, completes its request and ends; the
  next periodic run of the cluster watches on.
- Only an installed cluster is written Ready.

The job template has a time limit of 150 minutes. The operator treats a cluster's request as in flight while the
job's last check time is younger than 15 minutes (variable `in_flight_observed_stale_minutes` of the namespace
operator), so a job that died while it watched is replaced within about 25 minutes.

### Settings

Installation defaults, in the `platformopenshift_defaults` inventory variable under `install`:

| Key | Default | Meaning |
|---|---|---|
| `observeIntervalSeconds` | 30 | How often the job looks at the installation. |
| `watchWindowMinutes` | 120 | How long one job watches; keep it below the job template's time limit. |
| `deadlineMinutes` | 360 | An installation neither finished nor stopped after this long is failed (`InstallOverdue`). |
| `checkWriteMinutes` | 5 | How often the check time is written while nothing changes; keep it well below 15. |

The number of installer attempts is the `ClusterDeployment`'s `installAttemptsLimit` (3, set by the
`mce-cluster-build` chart).

## The cause a tenant sees

The job reads the cause of a failed attempt from the installer's log on the `ClusterProvision` (`spec.installLog`) and
from Hive's failure condition, with fixed patterns, and writes a sentence of its own: quota shortfalls with the
resource and the numbers (`QuotaExceeded`), a flavor, instance type or image the installer refused, named only when it
is the value the tenant set (`InstallSettingRefused`), refused credentials (`CredentialsInvalid`), an attempt that could
not start or left no log (`InstallNotStarted`), anything else (`InstallFailed`, with Hive's reason code when it is one
of Hive's known codes). No text of the log, of Hive's messages or of the site reaches the cluster object.

To read the whole log of a stopped installation, use the build name from the cluster's `status.buildName`:

```bash
oc get clusterprovisions -n <build name>
oc get clusterprovision <provision> -n <build name> -o jsonpath='{.spec.installLog}'
```

## Retry installation

A stopped installation is removed and installed again under the same name only when all of these hold, read fresh by
the job: the hub's build is not installed, Hive has stopped it, the cluster object records no installed cluster, and
the tenant asked after the stop (a new Retry request, or a spec change). The job records `CleaningUp`, reads the hub's
build once more, deletes the `ManagedCluster` and the `ClusterDeployment` (Hive deprovisions what the last attempt
built), uninstalls the release and installs again. The cluster keeps its floating IPs, DNS records, credential and
secret store paths. A failed attempt Hive is still retrying is never removed; nothing reinstalls by itself.

The API refuses a change of a cluster's installation settings (the provider block: node counts, flavors or instance
types, release, image and the rest the installer reads once, and `spec.type`) while the installation is cleaning up,
installing or finishing, and once the cluster is installed.

## What changes for existing clusters

- **Installed clusters:** nothing. They get `status.install.stage: Installed` at their next periodic run. Their
  installation settings can no longer be changed (they were never applied to an installed cluster).
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
