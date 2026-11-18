# Chart `quay`

The registry: the Quay operator's subscription, the QuayRegistry, its config bundle Secret and the db-init Sync hook
that merges the database URI, the storage and the sign-in settings into that bundle.

## Rollback

Roll back the chart **version** only. Never remove `ApplyOutOfSyncOnly=true` from the Application that installs
this chart, and never return to a chart version that renders the config bundle as `stringData` (0.6.4-shc.2 and
earlier): a full sync of that shape writes the chart's base `config.yaml` over the registry's live config bundle and
resets it (database URI, storage, sign-in settings and superusers added later are lost).
