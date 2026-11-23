{{- /* The platform's own health (platform-health.yaml): the alert rules as a named template, rendered only for the
switches that are on. */}}

{{- /* a Prometheus label reference in an alert text: {{ $labels.<name> }} */}}
{{- define "shc-cluster-base.label" -}}
{{ "{{" }} $labels.{{ . }} {{ "}}" }}
{{- end }}

{{- /* a CronJob whose newest run has not succeeded, or that has run but never succeeded; . = the series selector */}}
{{- define "shc-cluster-base.cronJobFailing" -}}
(max by (namespace, cronjob) (kube_cronjob_status_last_schedule_time{ {{- . -}} })
  > max by (namespace, cronjob) (kube_cronjob_status_last_successful_time{ {{- . -}} }))
or (max by (namespace, cronjob) (kube_cronjob_status_last_schedule_time{ {{- . -}} })
  unless on (namespace, cronjob) kube_cronjob_status_last_successful_time{ {{- . -}} })
{{- end }}

{{- define "shc-cluster-base.platformHealthEnabled" -}}
{{- $p := .Values.ops.platformHealth | default dict -}}
{{- range $k := list "volumes" "databaseBackups" "aggregatedAPIs" "receiver" "edaActivations" "identityWriteProbe" -}}
{{- if (get $p $k | default dict).enabled }}on{{ end -}}
{{- end -}}
{{- end }}

{{- define "shc-cluster-base.platformHealthRules" -}}
{{- $o := .Values.ops -}}
{{- $p := $o.platformHealth -}}
{{- $ns := printf "namespace=\"%s\"" $o.namespace -}}
{{- with $p.volumes }}{{ if .enabled }}
{{- $sel := printf "namespace=~\"%s\", namespace!~\"%s\"" .namespaces .excludeNamespaces }}
{{- $used := printf "(max by (namespace, persistentvolumeclaim) (kubelet_volume_stats_used_bytes{%s}) / max by (namespace, persistentvolumeclaim) (kubelet_volume_stats_capacity_bytes{%s}) * 100)" $sel $sel }}
{{- $rox := "unless on (namespace, persistentvolumeclaim) (max by (namespace, persistentvolumeclaim) (kube_persistentvolumeclaim_access_mode{access_mode=\"ReadOnlyMany\"}) == 1)" }}
- name: shc-platform-volumes
  rules:
    - alert: ShcVolumeFilling
      expr: >-
        {{ $used }} > {{ .warningPercent }} <= {{ .criticalPercent }}
        {{ $rox }}
      for: 15m
      labels:
        severity: warning
      annotations:
        summary: "Volume {{ include "shc-cluster-base.label" "namespace" }}/{{ include "shc-cluster-base.label" "persistentvolumeclaim" }} is {{ "{{" }} printf \"%.0f\" $value {{ "}}" }} % used"
        description: "The volume is over {{ .warningPercent }} % used. Find what grows on it (for a database: its write-ahead log waiting for a backup repository that is full or failing), or grow the volume."
    - alert: ShcVolumeAlmostFull
      expr: >-
        {{ $used }} > {{ .criticalPercent }}
        {{ $rox }}
      for: 5m
      labels:
        severity: critical
      annotations:
        summary: "Volume {{ include "shc-cluster-base.label" "namespace" }}/{{ include "shc-cluster-base.label" "persistentvolumeclaim" }} is {{ "{{" }} printf \"%.0f\" $value {{ "}}" }} % used"
        description: "The volume is over {{ .criticalPercent }} % used; what writes to it stops when it is full (a database stops and does not start again). Free space or grow the volume now, then find what fills it."
    - alert: ShcVolumeFillsSoon
      expr: >-
        max by (namespace, persistentvolumeclaim) (predict_linear(kubelet_volume_stats_available_bytes{ {{- $sel -}} }[{{ .rateWindow }}], {{ mul .fullWithinDays 86400 }})) < 0
        {{ $rox }}
      for: 1h
      labels:
        severity: critical
      annotations:
        summary: "Volume {{ include "shc-cluster-base.label" "namespace" }}/{{ include "shc-cluster-base.label" "persistentvolumeclaim" }} fills within {{ .fullWithinDays }} days at its present rate"
        description: "At the rate of the last {{ .rateWindow }} the volume is full within {{ .fullWithinDays }} days. Find what grows on it (for a database: its write-ahead log waiting for a backup repository that is full or failing) before it is full."
{{- end }}{{ end }}
{{- with $p.databaseBackups }}{{ if .enabled }}
{{- $cj := printf "namespace=~\"%s\", cronjob=~\".+-repo[0-9]+-(full|diff|incr)\"" .namespaces }}
{{- $suspended := "unless on (namespace, cronjob) (max by (namespace, cronjob) (kube_cronjob_spec_suspend) == 1)" }}
{{- $b := . }}
- name: shc-platform-database-backups
  rules:
    - alert: ShcDatabaseBackupFailed
      expr: >-
        ({{ include "shc-cluster-base.cronJobFailing" $cj | nindent 10 | trim }})
        {{ $suspended }}
      for: {{ .failedFor }}
      labels:
        severity: warning
      annotations:
        summary: "Database backup {{ include "shc-cluster-base.label" "namespace" }}/{{ include "shc-cluster-base.label" "cronjob" }}: the last run did not succeed"
        description: "The newest scheduled run of this backup has not succeeded for {{ .failedFor }}. The backup Job's log says why. A full or failing backup repository also holds back the database's write-ahead log, which then fills the database's own volume."
    - alert: ShcDatabaseBackupOverdue
      expr: >-
        (
        {{- range $i, $t := list "full" "diff" "incr" }}
          {{ if $i }}or {{ end }}(time() - max by (namespace, cronjob) (kube_cronjob_status_last_successful_time{namespace=~"{{ $b.namespaces }}", cronjob=~".+-repo[0-9]+-{{ $t }}"}) > {{ mul (get $b.overdueHours $t) 3600 }})
        {{- end }}
        )
        {{ $suspended }}
      for: 30m
      labels:
        severity: warning
      annotations:
        summary: "Database backup {{ include "shc-cluster-base.label" "namespace" }}/{{ include "shc-cluster-base.label" "cronjob" }}: no successful run for {{ "{{" }} $value | humanizeDuration {{ "}}" }}"
        description: "The backup has not succeeded for longer than its schedule allows, so a restore would go further back than planned and old backups do not expire. Check the CronJob's schedule and its latest Jobs."
    - alert: ShcDatabaseBackupNotScheduled
      expr: >-
        label_replace(max by (namespace, statefulset) (kube_statefulset_replicas{namespace=~"{{ .namespaces }}", statefulset=~".+-repo-host"}), "postgrescluster", "$1", "statefulset", "(.+)-repo-host")
        unless on (namespace, postgrescluster)
        label_replace(max by (namespace, cronjob) (kube_cronjob_info{namespace=~"{{ .namespaces }}", cronjob=~".+-repo[0-9]+-full"}), "postgrescluster", "$1", "cronjob", "(.+)-repo[0-9]+-full")
      for: {{ .notScheduledFor }}
      labels:
        severity: warning
      annotations:
        summary: "Database {{ include "shc-cluster-base.label" "namespace" }}/{{ include "shc-cluster-base.label" "postgrescluster" }} has no scheduled full backup"
        description: "The database has a backup repository but no scheduled full backup, so its old backups and write-ahead log never expire and the repository fills. Give the PostgresCluster a backup schedule and a retention."
{{- end }}{{ end }}
{{- with $p.aggregatedAPIs }}{{ if .enabled }}
- name: shc-platform-apis
  rules:
    - alert: ShcAggregatedAPIUnavailable
      expr: >-
        max by (name) (aggregator_unavailable_apiservice{name=~"{{ .names }}"}) == 1
      for: {{ .for }}
      labels:
        severity: critical
      annotations:
        summary: "Aggregated API {{ include "shc-cluster-base.label" "name" }} is unavailable"
        description: "The API server cannot reach the service behind this APIService. Meanwhile namespace deletes do not finish, and so cluster deletes hang. Check the APIService's conditions and the pods behind its service; restart a pod that runs but does not answer."
{{- end }}{{ end }}
{{- with $p.receiver }}{{ if .enabled }}
- name: shc-platform-alerting
  rules:
    - alert: ShcAlertReceiverMissing
      expr: >-
        max by (namespace) (alertmanager_integrations{namespace="{{ .namespace }}", job="{{ .job }}"}) == 0
      for: {{ .for }}
      labels:
        severity: warning
      annotations:
        summary: "The Alertmanager in {{ include "shc-cluster-base.label" "namespace" }} sends alerts nowhere"
        description: "No receiver of the cluster monitoring's Alertmanager has an integration (mail, webhook, chat or paging), so the platform's alerts are seen only on the console's alert page. Configure a receiver in the cluster monitoring's Alertmanager configuration."
{{- end }}{{ end }}
{{- with $p.edaActivations }}{{ if .enabled }}
- name: shc-platform-automation
  rules:
    - alert: ShcEventActivationsNotRunning
      expr: >-
        {{ include "shc-cluster-base.cronJobFailing" (printf "%s, cronjob=\"eda-activations\"" $ns) | nindent 8 | trim }}
      for: {{ .alertFor }}
      labels:
        severity: critical
      annotations:
        summary: "{{ include "shc-cluster-base.label" "namespace" }}/{{ include "shc-cluster-base.label" "cronjob" }}: an event activation is not running after repair, cannot run, or has two pods"
        description: "The activation check found activations it could not bring back within its repair limit, that cannot run as they are, or that run twice. Its last failed pod's termination message names each one. Fix the cause (the image, the project, the credential), then enable the activation again; of two Jobs of one activation, remove the older."
    - alert: ShcAutomationAPIUnavailable
      expr: >-
        {{ include "shc-cluster-base.cronJobFailing" (printf "%s, cronjob=\"eda-api-check\"" $ns) | nindent 8 | trim }}
      for: {{ .alertFor }}
      labels:
        severity: critical
      annotations:
        summary: "{{ include "shc-cluster-base.label" "namespace" }}/{{ include "shc-cluster-base.label" "cronjob" }}: the automation platform's event-automation API does not answer"
        description: "The API did not answer, answered an error, or refused the check's credential, so events are not handled and activations are neither checked nor repaired. Check the platform gateway, the automation platform's pods and its database; the check's last failed pod says what the API answered."
{{- end }}{{ end }}
{{- with $p.identityWriteProbe }}{{ if .enabled }}
- name: shc-platform-identity
  rules:
    - alert: ShcIdentityWriteRefused
      expr: >-
        {{ include "shc-cluster-base.cronJobFailing" (printf "%s, cronjob=~\"identity-write-probe-.+\"" $ns) | nindent 8 | trim }}
      for: {{ .alertFor }}
      labels:
        severity: critical
      annotations:
        summary: "{{ include "shc-cluster-base.label" "namespace" }}/{{ include "shc-cluster-base.label" "cronjob" }}: a Keycloak refuses a write"
        description: "The write check of this Keycloak failed: its database refused or lost a write, or the check could not sign in; the check's last failed pod says which. Sign-ins can still work while every change fails. If writes stay refused after a database fail-over, restart the Keycloak's pods so they connect to the new primary."
{{- end }}{{ end }}
{{- end }}
