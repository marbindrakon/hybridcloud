{{- define "shc-cluster-base.domain" -}}
{{- required "cluster.name is required" .Values.cluster.name }}.{{ required "cluster.baseDomain is required" .Values.cluster.baseDomain }}
{{- end }}

{{- define "shc-cluster-base.labels" -}}
app.kubernetes.io/name: shc-cluster-base
app.kubernetes.io/instance: {{ .Release.Name }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
{{- end }}

{{/*
Cluster singletons the chart only patches (APIServer, IngressController, CertManager,
ExternalSecretsConfig, the monitoring ConfigMap): server-side apply so only our fields are owned,
and never deleted on prune or Application removal.
*/}}
{{- define "shc-cluster-base.patchOnly" -}}
argocd.argoproj.io/sync-options: ServerSideApply=true,Delete=false,SkipDryRunOnMissingResource=true
{{- end }}
