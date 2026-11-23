# Platform services: what recovers by itself

For the platform team. What the platform's own services do after a fault without anybody acting, what still needs
a person, and the values that set each: the event activations of the automation platform, the Keycloak after a
database fail-over, and the platform's databases with their backups. The alerts that tell somebody when a service
does not come back are on their own page: [Platform alerts](platform-alerts.md).

| Service | Recovers by itself | Needs a person |
|---|---|---|
| Event activations | a clean exit or a failed one is restarted; past the platform's restart limit, the activation check restarts it, at most 3 times an hour | an activation the check cannot bring back, or one that cannot run as it is |
| Keycloak | after a database fail-over: within the connection lifetime, without a restart | a database that has no primary at all |
| Databases | a full backup weekly, a differential one daily, old backups expire; a repository that cannot take the archive costs the restore point, not the database | a restore; a repository that stays broken |

## Event activations

Every activation runs one rulebook that is meant to run for ever: it consumes the platform's events from Kafka. A
clean exit means its event source ended; a failed exit, that the process or its pod failed.

- **Restart policy `always`.** The automation platform restarts an activation after a clean exit and after a
  failed one. A clean exit is restarted at once and without a limit; a failed one after a delay (60 seconds by
  default) and within the restart limit below. Under `on-failure` an activation that exited cleanly stayed
  "completed" and its events were not handled until somebody restarted it.
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
  restart message). Nothing is set by default. That the platform operator passes these settings on next to the
  ones it sets itself is not proven for every operator version: try the setting on a test instance first.
- **The activation check** on the hub restarts an activation that is not running with one restart call, at most 3
  times an hour per activation by default, and raises an alert for one it cannot bring back. A restart does not reset
  the platform's count, so once the limit is reached each further failure waits for the check's next run. Its
  settings and alerts are on [Platform alerts](platform-alerts.md).
- **Changing an existing activation.** The platform refuses to change an activation while it is enabled ("Activation
  is not in disabled mode and in stopped status"), so a configuration run that changes, for example, the restart
  policy of running activations fails for each of them. The configuration run hides the module's messages (secure
  logging), so the job shows a censored failure rather than "Unable to update activation". Disable the activations
  first; the configuration run then changes and starts them, and the start resets their failure counts.
- **An activation that must stay off** needs `state: disabled` in its definition. The definition's `enabled` field
  does not decide it: without `state: disabled` every configuration run starts the activation.

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

Further server options of an installation go into `keycloak.additionalOptions`, entries `{name, value}` or
`{name, secret: {name, key}}`, rendered after the chart's own; the options the chart sets itself (`http-enabled`,
`hostname-strict`, `db-url-properties`, `db-pool-max-lifetime`) are refused there.

The two options are server options (`spec.additionalOptions` of the Keycloak), applied by the Keycloak operator as
environment variables of the Keycloak's pods: a change of either rolls the pods. The central chart's Applications
of the two Keycloaks no longer ignore that field, so a changed option reaches the Keycloak. Argo CD applies the
list as a whole: **before upgrading, move any option set by hand on a Keycloak object into
`keycloak.additionalOptions`**, or the first sync removes it and the Keycloak's pods roll without it.

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
  sets it (a plain number is bytes; 0 and more than 4PiB are refused); empty means no limit, and a broken
  repository then fills the data volume until the database stops.
  How long a broken repository takes to trip the limit: a database writes at least one 16 MiB segment a minute,
  so the limit in MiB divided by 16 is the longest time in minutes (2560MiB: under 3 hours; 20GiB: under 22
  hours); a busy database writes more and trips it sooner. On a data volume that is more than three quarters full
  the default is more than the free space, and the volume fills before the limit is reached: set a size below the
  free space, or grow the volume.
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

## Upgrading

An installation whose hub carried the Argo CD schema compatibility service (the APIService
`v1.schemafix.sovereign-bootstrap.io` in the namespace `argocd-schema-fix`) loses it when the init chart's release
is upgraded to this version: the upgrade deletes its namespace, Deployment, Service, configuration, service account,
cluster role and binding, and the APIService. Current versions of the multicluster engine publish no references the
service would have fixed, and while it was unavailable the hub's namespace deletes, and with them cluster deletes,
waited.

1. **Compare first.** Helm deletes every object of the release's stored manifest that the new render no longer
   contains. The init chart renders the namespaces for its pull secrets only when they do not exist yet, so a
   release installed before those namespaces existed has them in its manifest, and the new render does not: the
   upgrade would delete them with everything in them. Compare the stored manifest with the new render and keep
   every namespace the comparison would remove (a Helm resource policy), and look at every other change:

   The new render must come from the cluster (a server-side dry run), because the namespaces depend on what exists:

   ```sh
   helm get manifest sovereign-init -n openshift-gitops-operator > /tmp/init-stored.yaml
   helm upgrade sovereign-init bootstrap/helm/init -n openshift-gitops-operator \
     --reset-then-reuse-values --dry-run=server > /tmp/init-new.yaml
   diff <(grep -E '^(kind|  name|  namespace):' /tmp/init-stored.yaml) \
        <(grep -E '^(kind|  name|  namespace):' /tmp/init-new.yaml)
   oc annotate namespace <namespace> helm.sh/resource-policy=keep   # each namespace that must stay
   ```

2. **Delete the APIService first.** Helm deletes the objects in one pass and only logs a delete that fails; an
   APIService left behind without its server makes every namespace delete on the hub wait. Delete it yourself:

   ```sh
   oc delete apiservice v1.schemafix.sovereign-bootstrap.io
   ```

3. **Upgrade** the release from the repository's root, keeping its earlier settings (Helm 3.14 or later). The make
   target that installs or upgrades the init release (`make init-central-applicationset` in `bootstrap/`) runs the
   same upgrade without the comparison and without deleting the APIService: on a hub that carried the removed
   service, do steps 1 and 2 by hand before it.

   ```sh
   helm upgrade sovereign-init bootstrap/helm/init \
     --namespace openshift-gitops-operator \
     --reset-then-reuse-values \
     --wait --timeout=5m
   ```

4. **Check** that the APIService and the namespace are gone, and that a namespace delete completes:

   ```sh
   oc get apiservice v1.schemafix.sovereign-bootstrap.io     # NotFound
   oc get namespace argocd-schema-fix                        # NotFound, after a few minutes
   oc create namespace example-delete-check && oc delete namespace example-delete-check --wait
   ```

If a later version of the cluster engine brought such references back, Argo CD would fail to build its cache of the
hub: applications that deploy to the hub would show sync status Unknown, with a cluster cache error that names an
OpenAPI reference which cannot be resolved. Report it with that message.
