{{/*
True when the operator layer is enabled (OpenShift GitOps operator install).
*/}}
{{- define "sovereign-init.bootstrapOperator" -}}
{{- if .Values.bootstrap.operator }}true{{- end }}
{{- end }}

{{/*
True when bootstrap secrets layer is enabled (repo/cluster/OCI/pull/Gitea secrets).
*/}}
{{- define "sovereign-init.bootstrapSecrets" -}}
{{- if .Values.bootstrap.secrets }}true{{- end }}
{{- end }}

{{/*
True when ApplicationSet layer is enabled (central app-of-apps).
*/}}
{{- define "sovereign-init.bootstrapApplicationSet" -}}
{{- if .Values.bootstrap.applicationset }}true{{- end }}
{{- end }}

{{/*
True when the Argo CD schema compatibility shim is deployed: the operator layer is on and argocdSchemaFix.enabled
is not false (unset means on).
*/}}
{{- define "sovereign-init.schemaFixEnabled" -}}
{{- $sf := .Values.argocdSchemaFix | default dict -}}
{{- if and .Values.bootstrap.operator (or (not (hasKey $sf "enabled")) (kindIs "invalid" $sf.enabled) $sf.enabled) }}true{{- end }}
{{- end }}

{{/*
Expand the name of the chart.
*/}}
{{- define "sovereign-init.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "sovereign-init.labels" -}}
helm.sh/chart: {{ include "sovereign-init.name" . }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ include "sovereign-init.name" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: sovereign-bootstrap
{{- end }}
