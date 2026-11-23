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
| `aggregatedAPIs.enabled` | `ShcAggregatedAPIUnavailable` | every cluster that serves an aggregated API through a pod (an APIService with a service), where hanging namespace deletes matter |
| `edaActivations.enabled` | the CronJobs `eda-activations` and `eda-api-check`; `ShcEventActivationsNotRunning`, `ShcAutomationAPIUnavailable` | the cluster that runs the automation platform with event-driven automation |
| `identityWriteProbe.enabled` with `identityWriteProbe.keycloaks` | one CronJob `identity-write-probe-<name>` per Keycloak; `ShcIdentityWriteCheckFailed` | each cluster that runs a Keycloak of the platform |

The rules are in the PrometheusRule `shc-platform-health` in `ops.namespace` (default `shc-ops`); the checks run in
the same namespace. The operations layer must be rendered on the cluster (`layers.base`, or `layers.ops` alone).

## The alerts

| Alert | Severity | What it means | What to do |
|---|---|---|---|
| `ShcVolumeFilling` | warning | A volume in the watched namespaces is over `volumes.warningPercent` (80) % used for 15 minutes. | Find what grows on it. For a database, it is usually write-ahead log waiting for a backup repository that is full or failing. Otherwise grow the volume. |
| `ShcVolumeAlmostFull` | critical | Over `volumes.criticalPercent` (90) % used. | What writes to it stops when it is full, and a stopped database does not start again on a full volume. Free space or grow the volume now. |
| `ShcVolumeFillsSoon` | warning | Over `volumes.fillsSoonMinPercent` (50) % used and, at the rate of the last `volumes.rateWindow` (6 hours), full within `volumes.fullWithinDays` (3) days, for an hour. | As above, before it is full. A short burst on a volume that is mostly empty does not count; a trend alone is a warning, `ShcVolumeAlmostFull` is the critical one. |
| `ShcDatabaseBackupFailed` | warning | A scheduled backup of a database (CronJob `<database>-<repository>-full`, `-diff` or `-incr`) has not succeeded since its newest run, for `databaseBackups.failedFor` (3 hours), or has never succeeded. | Read the backup Job's log. A full or failing repository also holds back the database's write-ahead log, which then fills the database's own volume. |
| `ShcDatabaseBackupOverdue` | warning | No successful backup of that kind for longer than `databaseBackups.overdueHours` allows (full 192, differential and incremental 50 hours). | Check the CronJob's schedule and its latest Jobs. A restore would go further back than planned, and old backups do not expire. |
| `ShcDatabaseBackupNotScheduled` | warning | A database has a backup repository volume (its `<database>-repo-host` StatefulSet) but no scheduled full backup, for 6 hours. | Give the PostgresCluster a backup schedule and a retention. Without them, old backups and log never expire and the repository fills. |
| `ShcAggregatedAPIUnavailable` | critical | The API server cannot reach the service behind an APIService, and has not for `aggregatedAPIs.for` (10 minutes) without a break; or it could not for at least `aggregatedAPIs.unavailableFraction` (half) of the last `aggregatedAPIs.flapWindow` (an hour), held for 10 minutes. It fires while the fault lasts. The share of the window counts only for an APIService that has existed for a whole `flapWindow`: a new one that is not ready for its first minutes does not fire, and one that takes longer than `for` to become ready fires while it is not ready. After a long outage the share holds the alert past the recovery until less than the fraction of the last window was unavailable: after a 45-minute outage, about 30 minutes. | Meanwhile namespace deletes do not finish, so cluster deletes hang. Check the APIService's conditions and the pods behind its service; restart a pod that runs but does not answer. |
| `ShcEventActivationsNotRunning` | critical | The activation check reported something that needs a person (below), its run was incomplete, it has not succeeded for `edaActivations.staleMinutes` (60), its CronJob has not been scheduled for that long past its due time (also a CronJob that was never scheduled), or its CronJob is suspended. Quiet while the API check has failed every run for 8 minutes and was scheduled within `edaActivations.apiCheckScheduledWithinMinutes` (10) (then `ShcAutomationAPIUnavailable` speaks); not quiet for an API check that is suspended or no longer runs, also after a failed run, or that fails only every other run. | `oc -n <ops namespace> get jobs` shows the newest `eda-activations-<n>` Job; `oc -n <ops namespace> logs job/<that Job>` lists each finding with the activation's name. The ConfigMap `eda-activations-state` shows each activation's restarts and `gaveUp` (the platform refused its restart). Fix the cause (the image, the project, the credential); the check restarts the activation on its next run when the hour allows, or restart it yourself. Of two Jobs of one activation, remove the older. |
| `ShcAutomationAPIUnavailable` | critical | The event-automation API did not answer the check, answered an error, or refused the check's credential; or the API check has not succeeded for 60 minutes, has not been scheduled for that long past its due time, or is suspended. | Events are not handled and activations are neither checked nor repaired. Check the platform gateway, the automation platform's pods and its database. `oc -n <ops namespace> logs job/<the newest eda-api-check Job>` says what the API answered. |
| `ShcIdentityWriteCheckFailed` | critical | The identity write check failed: the Keycloak refused or lost a write to its database, the check could not get a token, or the Keycloak did not answer; or the check has not succeeded for `identityWriteProbe.staleMinutes` (60), has not been scheduled for that long past its due time, or is suspended. | The newest Job's log says which. Sign-ins can still work while every change fails. If writes stay refused after a database fail-over, restart the Keycloak's pods so that they connect to the new primary. |

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

`eda-activations` runs every 5 minutes (`edaActivations.schedule`). It reads its restart count, then lists the
activations through the event-automation API (`<url>/api/eda/v1/activations/`, every page).

**What it watches.** An activation that is enabled and has restart policy `always`. A disabled activation is never
touched and never reported, whoever disabled it. One with another restart policy is named once per run in the log
and left alone.

**What it does.** One call: `POST <url>/api/eda/v1/activations/<id>/restart/`, for a watched activation that, for
longer than its grace, either does not run (failed, stopped, completed, unresponsive, error) or reads running while
its newest instance does not (the platform lost track of it). The grace is `notRunningMinutes` (4): the activation
was seen so on an earlier run, so the platform's own restart goes first. Pending, starting and stopping get
`transitionalMinutes` (10), counted from when the check first saw it not running or last restarted it. Just before
the call the check reads that one activation again and restarts it only if it is still enabled and still due: a
person who disables it in between stops the restart, and one the platform has started working on in between is
left to the platform. After the restarts it waits up to `startTimeoutSeconds` (180) for them to run.

**What it never does.** It makes no call but reads and restart: it never disables, enables, changes or deletes an
activation (only with `removeStaleJobs: true` it deletes the older of two Jobs of one activation, see below).

**Its limits.**
- At most `maxRepairsPerHour` (3) restarts per activation in any hour. The check counts them over an hour plus the
  Job's deadline (70 minutes), because a restart in a slow run is made up to the deadline after it is counted. At
  the cap it reports the activation and restarts it again when that window allows.
- Every restart is counted in the ConfigMap `eda-activations-state` in `ops.namespace` before it is made, for every
  activation of a run before the first call. If the count cannot be stored, nothing is restarted and the run reports.
  If it cannot be read, or holds a state of the wrong shape, nothing is restarted and the run reports (a person
  repairs or deletes the ConfigMap). If another run wrote it meanwhile (two runs at once), the run stops without
  acting further and reports. A restart that was counted but not made (the activation changed in between) is taken
  off the count again. Each restart is stamped with the time its count is written. An activation missing from one
  read of the list keeps its count for a day; one listed twice in a read is judged once.
- More than `maxRepairsPerDay` (6) restarts of one activation within a day are reported although each one worked,
  so that an activation that keeps dying is looked at. The report holds until fewer than that are within the last
  24 hours, also after the activation runs steadily again.
- A restart the platform refuses as invalid or not allowed (400, 403) gives up on that activation: reported on
  every run, not restarted again until it is seen running or is disabled. Any other failed restart counts as an
  attempt and is reported.
- A run ends at the Job's deadline of 10 minutes. When many activations stop at once and the platform answers
  slowly, a run can reach it: the restarts it made are counted, those it counted but did not reach stay counted, and
  the rest continue on the next run. At the start of its restarts the run logs how many it will make and which.

**What it reports** (the run then exits with an error, which raises `ShcEventActivationsNotRunning`): an activation
at the cap, refused, or not running after its restart; one restarted too often in a day; one that cannot run as it
is (its status message, or its newest instance's, contains one of `cannotRunPatterns`: an image that cannot be
pulled, an invalid request); the platform's workers offline; a status the check does not know; two activation Jobs
with running pods for one watched activation (Jobs `activation-job-<activation>-<instance>` in
`automationNamespace`, the older one named; with `removeStaleJobs: true`, off by default, the older Job is removed,
but only when the activation and its newest instance run and the newest instance's Job runs); an empty activations
list (an account that cannot see them reads as one), unless `allowNoActivations` is true (an empty list never
clears the counts); and any read or write it needed that failed.

**The trade.** A restart does not reset the platform's own failure budget (the count after which the platform stops
restarting an activation by itself). Once that budget is used up, each further failure of the activation waits for
this check's next run, up to 5 minutes, instead of the platform's own restart a minute later. A configuration run
that disables and enables the activations is what resets the budget.

`eda-api-check` (every 5 minutes, `edaActivations.apiCheckSchedule`) makes one authenticated read of the activations
list. While it fails on every run, `ShcAutomationAPIUnavailable` fires and `ShcEventActivationsNotRunning` stays
quiet, so an outage of the automation platform raises one alert. An API check that is suspended or no longer runs
(for longer than `apiCheckScheduledWithinMinutes`, 10, whether its last run failed or not), or that fails only every
other run, does not quiet it.

**Stale checks.** `staleMinutes` (60) must be well above the interval of the check's schedule: a check that runs
less often than that would read as stale between its runs. A check whose next run has been due for longer than
`staleMinutes` (a CronJob the cluster never scheduled, or stopped scheduling) alerts too; on a fresh installation
that is never before its first run is that far overdue.

Both checks use https only, never follow a redirect, and follow a next-page link only on the same address, so the
credential goes nowhere else.

### What the installation provides

| Value | What |
|---|---|
| `edaActivations.url` | The platform gateway's base URL, https (for example `https://automation.example.com`). |
| `edaActivations.automationNamespace` | The namespace in which the automation platform runs the activation Jobs. The check may list Jobs there (and delete them only with `removeStaleJobs`). |
| `edaActivations.credentialSecret` | A Secret in `ops.namespace` with key `token` (a token of a platform account, sent as a bearer token) or keys `username` and `password`. |
| `edaActivations.caConfigMap` | Optional: a ConfigMap in `ops.namespace` with key `ca.crt` when the gateway's certificate is not from a public CA. |

The account needs to view the activations and restart them, and nothing else. The smallest built-in role that
allows this is the **Organization Operator** role of the organization that owns the activations (view of every
object; enable, disable and restart of activations). A custom role definition with only the activation permissions
`view` and `restart` is smaller still; the check needs neither enable nor disable.

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
  tokens for the client-credentials grant off). Keep its default client scopes (the `basic` scope puts the user's
  id, `sub`, into the token, which the check needs) and its default full scope;
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
