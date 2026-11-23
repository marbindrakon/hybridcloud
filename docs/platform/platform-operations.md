# Platform services: what recovers by itself

For the platform team. What the platform's own services do after a fault without anybody acting, what still needs
a person, and the values that set each: the event activations of the automation platform, the Keycloak after a
database fail-over, the schema compatibility service of the hub's Argo CD, and the platform's databases with their
backups. The alerts that tell somebody when a service does not come back are on their own page:
[Platform alerts](platform-alerts.md).

| Service | Recovers by itself | Needs a person |
|---|---|---|
| Event activations | a clean exit or a failed one is restarted | an activation past the platform's restart limit (see below) |
| Keycloak | after a database fail-over: within the connection lifetime, without a restart | a database that has no primary at all |
| Schema compatibility service | a server that stops answering is restarted; one silent client does not block it | a cluster where it is not needed: switch it off |
| Databases | a full backup weekly, a differential one daily, old backups expire; a repository that cannot take the archive costs the restore point, not the database | a restore; a repository that stays broken |

## Event activations

Every activation runs one rulebook that is meant to run for ever: it consumes the platform's events from Kafka. A
clean exit means its event source ended; a failed exit, that the process or its pod failed.

- **Restart policy `always`.** The automation platform restarts an activation after a clean exit and after a
  failed one. Under `on-failure` an activation that exited cleanly stayed "completed" and its events were not
  handled until somebody restarted it.
- **The restart limit.** The platform counts every failure of an activation over its life and stops restarting it
  once the count reaches its limit (5 by default): it then reads "failed ... has reached the maximum number of
  restarts". A restart does not reset the count; only disabling and enabling the activation does. You can raise the
  limit through the instance chart (`aap-instance`), which passes settings to the Event-Driven Ansible server:

  ```yaml
  eda:
    extraSettings:
      - setting: EDA_ACTIVATION_MAX_RESTARTS_ON_FAILURE
        value: "100"
  ```

  The server reads these settings from its environment and ignores a name it does not know without an error, so
  check the effect after a change (the variable on the server's pods, and the "(n/limit)" in an activation's
  restart message). Nothing is set by default.
- **The activation check** on the hub disables and enables an activation that is not running, at most a few times
  an hour, which also resets its count, and raises an alert for one it cannot bring back. Its settings and alerts
  are on [Platform alerts](platform-alerts.md).
- **Changing an existing activation.** The platform refuses to change an activation while it is enabled ("Activation
  is not in disabled mode and in stopped status"), so a configuration run that changes, for example, the restart
  policy of running activations reports "Unable to update activation" for each. Disable the activations first; the
  configuration run then changes and starts them, and the start resets their failure counts.

## The Keycloak after a database fail-over

The Keycloak reaches its database through the database's primary service. After a fail-over the service leads to
the new primary, but connections already open stay with the old one. When the old primary stayed up as a read-only
replica, those connections answered every check and failed every write.

Values of the `rhbk` chart:

| Value | Default | What it does |
|---|---|---|
| `keycloak.db.urlProperties` | `auto` | Appended to the database's JDBC URL (Keycloak's `db-url-properties`), with its leading `?`. `auto`: with the chart's own database (`keycloak.db.crunchy.enabled`), `?targetServerType=primary`: the driver accepts only a primary, so a connection opened to a server that is no longer primary is refused and opened again. With another database nothing is added. Empty: nothing. |
| `keycloak.db.poolMaxLifetime` | `10m` | The longest a database connection lives (Keycloak's `db-pool-max-lifetime`; `10m`, `600` for seconds, or `PT10M`). A connection is closed when it is returned after this time, so a stale connection is gone within it, whatever made it stale. Empty: no limit. |
| `keycloak.db.crunchy.patroniFailsafe` | `false` | Patroni's fail-safe mode on the chart's database: a primary that cannot reach the Kubernetes API but still reaches every other member stays primary instead of making itself read-only. It changes how every fail-over of that database is decided: try it on a test environment first. |

What it does not cover: a request that is running on a connection when the primary goes away fails, as it would
without these settings; and the Keycloak's readiness probe does not test a write, so ready does not prove that
writes succeed.

The two options are server options (`spec.additionalOptions` of the Keycloak), applied by the Keycloak operator as
environment variables of the Keycloak's pods: a change of either rolls the pods. The central chart's Applications
of the two Keycloaks no longer ignore that field, so a changed option reaches the Keycloak.

## The schema compatibility service

On a hub with the multicluster engine, Argo CD fails to resolve some `~1`-encoded references of the cluster's
aggregated OpenAPI document. The init chart can deploy a small HTTPS server behind the APIService
`v1.schemafix.sovereign-bootstrap.io` that serves the definitions those references need. Its APIService is an
aggregated API: while it is unavailable, API discovery is incomplete and namespace deletes, and with them cluster
deletes, wait.

How it recovers:

- each connection is served on its own, with a timeout on the TLS handshake and on every read, so a client that
  connects and stays silent costs only its own connection;
- a startup probe allows the first load of the cluster's OpenAPI documents, then a liveness probe (a real HTTPS
  request) restarts a server that stopped answering;
- two replicas, spread over nodes where the cluster has several, a rolling update that keeps one Ready, and a
  disruption budget that keeps one through a node drain.

Values of the init chart (`argocdSchemaFix`):

| Value | Default | What it does |
|---|---|---|
| `enabled` | `true` | `false` renders none of its objects: its namespace with its pull secret, service account, cluster role and binding, configuration, Deployment, Service, disruption budget and APIService. |
| `replicas` | `2` | Server pods; with more than one, a disruption budget keeps one. |
| `handshakeTimeoutSeconds`, `readTimeoutSeconds` | `10`, `30` | How long a connection may take for its handshake, then for each read or write. |
| `startupAllowanceSeconds` | `600` | How long a starting pod may take to load the documents and answer before it is restarted. |
| `resources` | requests 50m CPU, 256Mi; limit 1Gi | The server holds the cluster's aggregated OpenAPI document in memory while it refreshes. |

Switch it off where Argo CD's cluster cache syncs without it. If Argo CD then fails on a `~1` reference again,
switch it back on.

The init chart is installed with Helm, not by Argo CD, so a change reaches a running hub only with an upgrade of
its release. Keep the release's earlier settings and take the new chart's defaults (Helm 3.14 or later):

```sh
helm upgrade sovereign-init bootstrap/helm/init \
  --namespace openshift-gitops-operator \
  --reset-then-reuse-values \
  --set argocdSchemaFix.enabled=false \
  --wait --timeout=5m
```

Leave out the `--set` line to keep the service and take the hardened server. The upgrade replaces the server's pods
one at a time (a new one Ready before an old one goes); no other pod of the release restarts. Switched off, the
upgrade deletes the APIService, the Deployment and the rest, and the namespace with them.

## Databases and their backups

Each of the platform's PostgreSQL databases (the automation platform's, the Keycloaks', Gitea's, Quay's) is backed
up by pgBackRest into a repository volume of its own.

- **Schedule.** A full backup weekly (`fullSchedule`) and a differential one on the other days
  (`differentialSchedule`; empty: none), cron in UTC, each chart at its own minute.
- **Retention.** `retentionFull` full backups are kept (default 2), with the differential backups and the
  write-ahead log that follow them, and of the differential backups the newest `retentionDifferential` (default 2).
  Older ones expire when a backup completes.
- **The restore window.** A database can be restored to any moment since the oldest kept full backup: with the
  defaults, between 7 and 14 days back. The first expiry comes with the second scheduled full backup after the
  settings reach a database.
- **The waiting archive** (`archivePushQueueMax`). When the write-ahead log cannot be archived, for example because
  the repository is full or unreachable, the segments wait in the database's data volume. Past this size they are
  dropped: the database's log says `dropped WAL file '...' because archive queue exceeded ...`, and the database
  keeps running instead of filling its data volume and stopping. The cost is the restore point: the database cannot
  be restored to a moment between the drop and the next completed backup. Because archiving is asynchronous, the
  whole waiting queue is dropped at once when it passes the limit, and the limit is checked when an archiving run
  starts, so the queue can pass it briefly. `auto` (the default) is a quarter of the data volume (`storage` must then
  be a whole number of Mi, Gi or Ti, or the chart refuses to render and asks for the value); a size such as `4GiB`
  sets it; empty means no limit, and a broken repository then fills the data volume until the database stops.
- **A full backup on demand.** Annotate the database's PostgresCluster; each new value of the annotation starts one
  full backup:

  ```sh
  oc annotate postgrescluster <name> -n <namespace> --overwrite \
    postgres-operator.crunchydata.com/pgbackrest-backup="$(date -u +%Y%m%dT%H%M%SZ)"
  ```

  Take one after a repository fault: it gives the database a restore point again and lets old backups expire.

Size the repository volume for `retentionFull` + 1 full backups, `retentionDifferential` differential ones and the
write-ahead log of `retentionFull` + 1 full intervals.

Where the settings are, by chart:

| Chart | Data volume size | Backup settings |
|---|---|---|
| `aap-instance` | `postgres.storage` | `postgres.backup.*` |
| `aap` | `postgres.storage` | `postgres.backup.*` |
| `rhbk` | `keycloak.db.crunchy.storage` | `keycloak.db.crunchy.backup.*` |
| `gitea` | `crunchy.storage` | `crunchy.backup.*` |
| `quay` | `crunchy.storage` | `crunchy.backup.*` |

What does not recover by itself: a restore is a person's decision and work, and a repository that stays broken keeps
the database without new restore points until it is repaired; the volume and backup alerts on
[Platform alerts](platform-alerts.md) say when that happens.
