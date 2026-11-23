# Platform alerts

The platform reports faults in its own services through the monitoring of the cluster each service runs on: alert
rules where a metric exists, and two small checks where none does. The checks are CronJobs of the platform's
operations layer (the `shc-cluster-base` chart). A failed run of a check is its alert. Everything on this page is
off by default: an installation switches on, per cluster, the parts that apply there.

An alert reaches a person only through a **receiver** of the cluster monitoring's Alertmanager. The platform does
not set up a receiver. Until an installation configures one, alerts show only on the console's alert page, and
`ShcAlertReceiverMissing` says so (see [Receivers](#receivers)).

## Which part runs where

| Values under `ops.platformHealth` | Turns on | Typical clusters |
|---|---|---|
| `volumes.enabled` | `ShcVolumeFilling`, `ShcVolumeAlmostFull`, `ShcVolumeFillsSoon` | every cluster that holds platform databases or other platform volumes |
| `databaseBackups.enabled` | `ShcDatabaseBackupFailed`, `ShcDatabaseBackupOverdue`, `ShcDatabaseBackupNotScheduled` | every cluster with a platform PostgreSQL database |
| `aggregatedAPIs.enabled` | `ShcAggregatedAPIUnavailable` | the cluster that runs the platform's own aggregated API, and any cluster where hanging deletes matter |
| `receiver.enabled` | `ShcAlertReceiverMissing` | every cluster whose alerts are not forwarded to another Alertmanager |
| `edaActivations.enabled` | the CronJobs `eda-activations` and `eda-api-check`; `ShcEventActivationsNotRunning`, `ShcAutomationAPIUnavailable` | the cluster that runs the automation platform with event-driven automation |
| `identityWriteProbe.enabled` with `identityWriteProbe.keycloaks` | one CronJob `identity-write-probe-<name>` per Keycloak; `ShcIdentityWriteRefused` | each cluster that runs a Keycloak of the platform |

The rules are in the PrometheusRule `shc-platform-health` in `ops.namespace` (default `shc-ops`); the checks run in
the same namespace. The operations layer must be rendered on the cluster (`layers.base`, or `layers.ops` alone).

## The alerts

| Alert | Severity | What it means | What to do |
|---|---|---|---|
| `ShcVolumeFilling` | warning | A volume in the watched namespaces is over `volumes.warningPercent` (80) % used for 15 minutes. | Find what grows on it. For a database, it is usually write-ahead log waiting for a backup repository that is full or failing. Otherwise grow the volume. |
| `ShcVolumeAlmostFull` | critical | Over `volumes.criticalPercent` (90) % used. | What writes to it stops when it is full, and a stopped database does not start again on a full volume. Free space or grow the volume now. |
| `ShcVolumeFillsSoon` | critical | At the rate of the last `volumes.rateWindow` (6 hours), the volume is full within `volumes.fullWithinDays` (3) days, for an hour. | As above, before it is full. This alert fires days before a percentage threshold when a database's archive stops. |
| `ShcDatabaseBackupFailed` | warning | A scheduled backup of a database (CronJob `<database>-<repository>-full`, `-diff` or `-incr`) has not succeeded since its newest run, for `databaseBackups.failedFor` (3 hours), or has never succeeded. | Read the backup Job's log. A full or failing repository also holds back the database's write-ahead log, which then fills the database's own volume. |
| `ShcDatabaseBackupOverdue` | warning | No successful backup of that kind for longer than `databaseBackups.overdueHours` allows (full 192, differential and incremental 50 hours). | Check the CronJob's schedule and its latest Jobs. A restore would go further back than planned, and old backups do not expire. |
| `ShcDatabaseBackupNotScheduled` | warning | A database has a backup repository volume (its `<database>-repo-host` StatefulSet) but no scheduled full backup, for 6 hours. | Give the PostgresCluster a backup schedule and a retention. Without them, old backups and log never expire and the repository fills. |
| `ShcAggregatedAPIUnavailable` | critical | The API server cannot reach the service behind an APIService for `aggregatedAPIs.for` (10 minutes). | Meanwhile namespace deletes do not finish, so cluster deletes hang. Check the APIService's conditions and the pods behind its service; restart a pod that runs but does not answer. |
| `ShcAlertReceiverMissing` | warning | The cluster monitoring's Alertmanager has no receiver with an integration, for an hour. | Configure a receiver (see [Receivers](#receivers)). This alert is the visible sign that alerts go nowhere; it is quiet otherwise. |
| `ShcEventActivationsNotRunning` | critical | The activation check found an activation it could not bring back within its repair limit, one that cannot run as it is, or one that runs twice. | The check's last failed pod names each activation in its termination message (`oc -n <ops namespace> get pods`, then the pod's status). Fix the cause (its image, its project, its credential), then enable the activation again. Of two Jobs of one activation, remove the older. |
| `ShcAutomationAPIUnavailable` | critical | The event-automation API did not answer the check, answered an error, or refused the check's credential. | Events are not handled and activations are neither checked nor repaired. Check the platform gateway, the automation platform's pods and its database. The failed pod says what the API answered. |
| `ShcIdentityWriteRefused` | critical | The write check of a Keycloak failed: its database refused or lost a write, or the check could not sign in. | The pod says which. Sign-ins can still work while every change fails. If writes stay refused after a database fail-over, restart the Keycloak's pods so that they connect to the new primary. |

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
event-automation API (`<url>/api/eda/v1/activations/`). For each enabled activation it reads the newest instance.
Then:

- **Running** (and its newest instance running): nothing to do.
- **Not running** (failed, stopped, completed, unresponsive, error), or **running while its newest instance is
  not**: seen on two runs at least `notRunningMinutes` (4) apart, so the platform's own restart goes first, it is
  disabled, the check waits until it has stopped (`stopTimeoutSeconds`), enables it again and waits until it runs
  (`startTimeoutSeconds`). Enabling resets the platform's own failure count. Each repair is logged with the
  activation's name and counted. All repairs of one run are done together, so a whole platform's activations come
  back in one wait.
- **Pending, starting or stopping** for longer than `transitionalMinutes` (10): treated as not running.
- **At most `maxRepairsPerHour` (3) repairs per activation within one hour.** Past that it is reported and left
  alone until it is seen running again with no repair in the last hour.
- **Cannot run as it is**: its status message, or its newest instance's, contains one of `cannotRunPatterns` (an
  image that cannot be pulled, an invalid request); or the platform refuses to enable it as invalid. Reported,
  never repaired.
- **The platform's workers are offline**, or **a status the check does not know**: reported, not repaired.
- **Two activation Jobs with running pods for one activation** (Jobs `activation-job-<activation>-<instance>` in
  `automationNamespace`): reported, naming the older Job. With `removeStaleJobs: true` (off by default) the older
  Job is removed, but only when the activation and its newest instance run and the newest instance's Job runs.
- **An activation this check disabled and could not enable again** is enabled again on the next run, within the
  same limit, and reported until it runs; it is never left disabled without a report.

The repair count is kept in the ConfigMap `eda-activations-state` in `ops.namespace`, so it holds across runs and
outside the automation platform. When that ConfigMap cannot be read, nothing is repaired and the run is reported.
Deleting it resets every count.

`eda-api-check` (every 5 minutes, `edaActivations.apiCheckSchedule`) makes one authenticated read of the
activations list. When the API does not answer, `eda-activations` repairs nothing and leaves the report to this
check, so an outage of the automation platform raises one alert.

### What the installation provides

| Value | What |
|---|---|
| `edaActivations.url` | The platform gateway's base URL (for example `https://automation.example.com`). |
| `edaActivations.automationNamespace` | The namespace in which the automation platform runs the activation Jobs. The check may list Jobs there (and delete them only with `removeStaleJobs`). |
| `edaActivations.credentialSecret` | A Secret in `ops.namespace` with key `token` (a token of a platform account, sent as a bearer token) or keys `username` and `password`. |
| `edaActivations.caConfigMap` | Optional: a ConfigMap in `ops.namespace` with key `ca.crt` when the gateway's certificate is not from a public CA. |

The account needs to view, enable and disable the activations, and nothing else. The smallest built-in role that
allows this is the **Organization Operator** role of the organization that owns the activations (view of every
object, enable, disable and restart of activations). A custom role definition with only the activation permissions
`view`, `enable` and `disable` is smaller still.

## The Keycloak write check

`identity-write-probe-<name>` runs every 5 minutes for each entry of `identityWriteProbe.keycloaks`. A dedicated,
ordinary user of a realm signs in through a dedicated client (the password grant), reads its own account through
the Account API, writes its own last name (or `attribute`) with the current time, reads it back, and signs out. The
write changes only that user and needs only the user's own `manage-account` role; no administration right is
involved. A service account cannot be used for this: the Account API refuses service accounts.

| Field of an entry | What |
|---|---|
| `name` | Short name of the Keycloak (lower-case letters, digits and `-`, at most 31 characters); the CronJob is `identity-write-probe-<name>`. |
| `url` | The Keycloak's base URL. |
| `realm`, `clientId` | Where the user and the client are. |
| `credentialSecret` | A Secret in `ops.namespace` with keys `username`, `password` and, for a confidential client, `clientSecret`. |
| `attribute` | Optional: `lastName` (default), `firstName`, or an attribute the realm's user profile lets the user edit. |
| `caConfigMap` | Optional: a ConfigMap with key `ca.crt` for a private CA. |

Create in that Keycloak, in a realm that no tenant administers:

- a client with only **Direct access grants** on (standard flow and service accounts off), confidential, with the
  default client scopes;
- a user with first name, last name and email set, the email verified, no required actions, a password that is not
  temporary, and the role `manage-account` of the realm's `account` client (part of the realm's default roles) and
  no other role.

The check makes one sign-in session per run and ends it again.

## Receivers

Alerts of the platform's rules go to the cluster monitoring's own Alertmanager (in `openshift-monitoring`). The
installation configures where that Alertmanager sends them: in the OpenShift console under Administration, Cluster
Settings, Configuration, Alertmanager, or in the Secret `alertmanager-main` of `openshift-monitoring` (key
`alertmanager.yaml`). Route at least the `critical` and `warning` alerts to a receiver with an integration (mail,
a webhook, a chat or a paging service), and keep the `Watchdog` alert on a receiver of its own. The platform's
alerts all carry `severity` and name the object in their labels (`namespace`, `persistentvolumeclaim`, `cronjob`,
`name`).

`ShcAlertReceiverMissing` reads only the number of integrations the Alertmanager counts for receivers that a route
uses (its metric `alertmanager_integrations`); it never reads the configuration, which can hold secrets. A receiver
used only by the `Watchdog` route also counts. On a cluster whose alerts are forwarded to another Alertmanager that
has the receivers, switch `receiver.enabled` off there.
