# Platform alerts

The platform reports faults in its own services through the monitoring of the cluster each service runs on: alert
rules where a metric exists, and two small checks where none does. The checks are CronJobs of the platform's
operations layer (the `shc-cluster-base` chart). A failed run of a check is its alert. Everything on this page is
off by default: an installation switches on, per cluster, the parts that apply there.

An alert reaches a person only through a **receiver** of the cluster monitoring's Alertmanager. The platform does
not set up a receiver. Until an installation configures one, alerts show only on the console's alert page, and
OpenShift's own alert `AlertmanagerReceiversNotConfigured` says so (see [Receivers](#receivers)).

## Which part runs where

| Values under `ops.platformHealth` | Turns on | Typical clusters |
|---|---|---|
| `volumes.enabled` | `ShcVolumeFilling`, `ShcVolumeAlmostFull`, `ShcVolumeFillsSoon` | every cluster that holds platform databases or other platform volumes |
| `databaseBackups.enabled` | `ShcDatabaseBackupFailed`, `ShcDatabaseBackupOverdue`, `ShcDatabaseBackupNotScheduled` | every cluster with a platform PostgreSQL database |
| `aggregatedAPIs.enabled` | `ShcAggregatedAPIUnavailable` | the cluster that runs the platform's own aggregated API, and any cluster where hanging deletes matter |
| `edaActivations.enabled` | the CronJobs `eda-activations` and `eda-api-check`; `ShcEventActivationsNotRunning`, `ShcAutomationAPIUnavailable` | the cluster that runs the automation platform with event-driven automation |
| `identityWriteProbe.enabled` with `identityWriteProbe.keycloaks` | one CronJob `identity-write-probe-<name>` per Keycloak; `ShcIdentityWriteCheckFailed` | each cluster that runs a Keycloak of the platform |

The rules are in the PrometheusRule `shc-platform-health` in `ops.namespace` (default `shc-ops`); the checks run in
the same namespace. The operations layer must be rendered on the cluster (`layers.base`, or `layers.ops` alone).

## The alerts

| Alert | Severity | What it means | What to do |
|---|---|---|---|
| `ShcVolumeFilling` | warning | A volume in the watched namespaces is over `volumes.warningPercent` (80) % used for 15 minutes. | Find what grows on it. For a database, it is usually write-ahead log waiting for a backup repository that is full or failing. Otherwise grow the volume. |
| `ShcVolumeAlmostFull` | critical | Over `volumes.criticalPercent` (90) % used. | What writes to it stops when it is full, and a stopped database does not start again on a full volume. Free space or grow the volume now. |
| `ShcVolumeFillsSoon` | critical | Over `volumes.fillsSoonMinPercent` (50) % used and, at the rate of the last `volumes.rateWindow` (6 hours), full within `volumes.fullWithinDays` (3) days, for an hour. | As above, before it is full. A short burst on a volume that is mostly empty (a backup into its repository) does not count. |
| `ShcDatabaseBackupFailed` | warning | A scheduled backup of a database (CronJob `<database>-<repository>-full`, `-diff` or `-incr`) has not succeeded since its newest run, for `databaseBackups.failedFor` (3 hours), or has never succeeded. | Read the backup Job's log. A full or failing repository also holds back the database's write-ahead log, which then fills the database's own volume. |
| `ShcDatabaseBackupOverdue` | warning | No successful backup of that kind for longer than `databaseBackups.overdueHours` allows (full 192, differential and incremental 50 hours). | Check the CronJob's schedule and its latest Jobs. A restore would go further back than planned, and old backups do not expire. |
| `ShcDatabaseBackupNotScheduled` | warning | A database has a backup repository volume (its `<database>-repo-host` StatefulSet) but no scheduled full backup, for 6 hours. | Give the PostgresCluster a backup schedule and a retention. Without them, old backups and log never expire and the repository fills. |
| `ShcAggregatedAPIUnavailable` | critical | The API server could not reach the service behind an APIService for more than `aggregatedAPIs.unavailableFraction` (half) of the last `aggregatedAPIs.window` (10 minutes), held for `aggregatedAPIs.for` (5 minutes). An API that keeps dropping out counts too. | Meanwhile namespace deletes do not finish, so cluster deletes hang. Check the APIService's conditions and the pods behind its service; restart a pod that runs but does not answer. |
| `ShcEventActivationsNotRunning` | critical | The activation check reported something that needs a person (below), its run was incomplete, it has not succeeded for `edaActivations.staleMinutes` (60), or its CronJob is suspended. Quiet while `ShcAutomationAPIUnavailable` holds. | `oc -n <ops namespace> get jobs` shows the newest `eda-activations-<n>` Job; `oc -n <ops namespace> logs job/<that Job>` lists each finding with the activation's name. The ConfigMap `eda-activations-state` shows each activation's repairs and whether the check gave up on it. Fix the cause (the image, the project, the credential), then enable the activation again. Of two Jobs of one activation, remove the older. |
| `ShcAutomationAPIUnavailable` | critical | The event-automation API did not answer the check, answered an error, or refused the check's credential; or the API check has not succeeded for 60 minutes, or is suspended. | Events are not handled and activations are neither checked nor repaired. Check the platform gateway, the automation platform's pods and its database. `oc -n <ops namespace> logs job/<the newest eda-api-check Job>` says what the API answered. |
| `ShcIdentityWriteCheckFailed` | critical | The identity write check failed: the Keycloak refused or lost a write to its database, the check could not get a token, or the Keycloak did not answer; or the check has not succeeded for `identityWriteProbe.staleMinutes` (60), or is suspended. | The newest Job's log says which. Sign-ins can still work while every change fails. If writes stay refused after a database fail-over, restart the Keycloak's pods so that they connect to the new primary. |

The generic job alerts of the operations layer (`ShcBackupJobFailed` and the like) leave the checks' CronJobs out,
so a failed check raises one alert.

### Volumes and backups: settings

- `volumes.namespaces` and `volumes.excludeNamespaces` are regular expressions. The default watches every
  namespace except OpenShift's own (`openshift-.*`, `kube-.*`, `default`), which OpenShift's own volume alert
  covers. Volumes mounted read-only by many pods are left out.
- `databaseBackups.namespaces` limits the backup alerts. A database that is shut down or a standby has its backup
  CronJobs suspended; suspended CronJobs never alert.
- Keep each `overdueHours` above its schedule's period plus the time a backup takes.

## The activation check

`eda-activations` runs every 5 minutes (`edaActivations.schedule`). It lists the activations through the
event-automation API (`<url>/api/eda/v1/activations/`, every page). It watches the activations whose restart policy
is `always`; others are not watched, and the log names them once per run. For each watched, enabled activation it
reads the newest instance. Then:

- **Running** (and its newest instance running): nothing to do.
- **Not running** (failed, stopped, completed, unresponsive, error), or **running while its newest instance is
  not**: seen on two runs at least `notRunningMinutes` (4) apart, so the platform's own restart goes first. The
  check reads the activation again; if it still does not run, the check records the repair (below), disables it,
  waits until it has stopped (`stopTimeoutSeconds`), enables it again and waits until it runs
  (`startTimeoutSeconds`). Enabling resets the platform's own failure count. Each repair is logged with the
  activation's name and counted. All repairs of one run are done together, so a whole platform's activations come
  back in one wait. After a repair the waiting time starts again, so a slow start is not repaired again on the next
  run.
- **Pending, starting or stopping** for longer than `transitionalMinutes` (10): treated as not running.
- **At most `maxRepairsPerHour` (3) repairs per activation in any hour.** Past that it is reported and left alone
  until it is seen running with no repair in the last hour.
- **Repaired more than `maxRepairsPerDay` (6) times in a day**: reported, although each repair worked, so that an
  activation that keeps dying is looked at.
- **Cannot run as it is**: its status message, or its newest instance's, contains one of `cannotRunPatterns` (an
  image that cannot be pulled, an invalid request): reported, never repaired. When the platform refuses to enable
  it as invalid, it stays disabled and is reported each run until a person enables it.
- **The platform's workers are offline**, or **a status the check does not know**: reported, not repaired.
- **Two activation Jobs with running pods for one activation** (Jobs `activation-job-<activation>-<instance>` in
  `automationNamespace`): reported, naming the older Job. With `removeStaleJobs: true` (off by default) the older
  Job is removed, but only when the activation and its newest instance run and the newest instance's Job runs.
- **An empty activations list** is reported (an account that cannot see the activations reads as an empty list),
  unless `allowNoActivations` is true. Nothing is changed then.

**The record comes first.** The repair count is kept in the ConfigMap `eda-activations-state` in `ops.namespace`.
Before the check disables or enables anything, it writes there the counted repair and the mark that it disabled
the activation itself; if that write fails, it changes nothing and reports. So whatever stops a run midway (an API
error, its deadline, the pod evicted), an activation the check disabled is either enabled again in the same run or
marked, and the next run enables it again, within the same hourly limit. An activation disabled by anyone else is
never enabled. When the ConfigMap cannot be read, nothing is repaired and the run is reported. Deleting it resets
every count.

The run exits with an error (and so raises `ShcEventActivationsNotRunning`) whenever something needs a person or
the run was incomplete: a finding above, a repair that did not finish, or a read or write it needed that failed.

`eda-api-check` (every 5 minutes, `edaActivations.apiCheckSchedule`) makes one authenticated read of the
activations list. While it fails, `ShcAutomationAPIUnavailable` fires and `ShcEventActivationsNotRunning` stays
quiet, so an outage of the automation platform raises one alert.

Both checks use https only, never follow a redirect, and follow a next-page link only on the same address, so the
credential goes nowhere else.

### What the installation provides

| Value | What |
|---|---|
| `edaActivations.url` | The platform gateway's base URL, https (for example `https://automation.example.com`). |
| `edaActivations.automationNamespace` | The namespace in which the automation platform runs the activation Jobs. The check may list Jobs there (and delete them only with `removeStaleJobs`). |
| `edaActivations.credentialSecret` | A Secret in `ops.namespace` with key `token` (a token of a platform account, sent as a bearer token) or keys `username` and `password`. |
| `edaActivations.caConfigMap` | Optional: a ConfigMap in `ops.namespace` with key `ca.crt` when the gateway's certificate is not from a public CA. |

The account needs to view, enable and disable the activations, and nothing else. The smallest built-in role that
allows this is the **Organization Operator** role of the organization that owns the activations (view of every
object, enable, disable and restart of activations). A custom role definition with only the activation permissions
`view`, `enable` and `disable` is smaller still.

## The Keycloak write check

`identity-write-probe-<name>` runs every 5 minutes for each entry of `identityWriteProbe.keycloaks`. In a dedicated
realm of that Keycloak, a client's service account takes a token (the client-credentials grant), reads its own user
through the realm's Admin API, writes its own last name (or `attribute`) with the current time, and reads it back.
The token request leaves no session behind (without refresh tokens for the client-credentials grant, its session is
kept in memory only and not stored).

| Field of an entry | What |
|---|---|
| `name` | Short name of the Keycloak (lower-case letters, digits and `-`, at most 31 characters); the CronJob is `identity-write-probe-<name>`. |
| `url` | The Keycloak's base URL, https. |
| `realm`, `clientId` | The dedicated realm and the client in it. |
| `credentialSecret` | A Secret in `ops.namespace` with key `clientSecret`. |
| `attribute` | Optional: `lastName` (default), `firstName`, or another attribute. |
| `caConfigMap` | Optional: a ConfigMap with key `ca.crt` for a private CA. |

Create in that Keycloak:

- **a realm of its own for this check**, one that no application trusts and nobody signs in to (no other client,
  no identity provider, no users);
- in it, a confidential client with only **Service accounts** on (standard flow, direct access grants and refresh
  tokens for the client-credentials grant off);
- for the client's service account, the role `manage-users` of that realm's `realm-management` client, and no
  other role.

**What a holder of the Secret can do:** administer the users of that empty realm, and nothing else: no other realm,
no client, no sign-in anywhere an application would accept. The service account's own user is the only user there;
its last name is the field the check writes. The Admin API accepts that change for a service account's user without
any user profile configuration.

**What each run leaves:** if the realm stores login events, one `CLIENT_LOGIN` event; if it stores admin events, one
`UPDATE` of the user. With event storage off (the default), nothing but the changed last name.

## Receivers

Alerts of the platform's rules go to the cluster monitoring's own Alertmanager (in `openshift-monitoring`). The
installation configures where that Alertmanager sends them: in the OpenShift console under Administration, Cluster
Settings, Configuration, Alertmanager, or in the Secret `alertmanager-main` of `openshift-monitoring` (key
`alertmanager.yaml`). Route at least the `critical` and `warning` alerts to a receiver with an integration (mail,
a webhook, a chat or a paging service), and keep the `Watchdog` alert on a receiver of its own. The platform's
alerts all carry `severity` and name the object in their labels (`namespace`, `persistentvolumeclaim`, `cronjob`,
`name`).

While no receiver has an integration, OpenShift's own alert `AlertmanagerReceiversNotConfigured` (warning) fires
on the cluster. It is the visible sign that alerts go nowhere; the platform adds no alert of its own for it. On a
cluster whose alerts are forwarded to another Alertmanager that has the receivers, that alert is expected.
