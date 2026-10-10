{{- define "tenancy-dashboard.name" -}}
tenancy-dashboard
{{- end -}}

{{- define "tenancy-dashboard.labels" -}}
app.kubernetes.io/name: {{ include "tenancy-dashboard.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: hybrid-sovereign-cloud
{{- end -}}

{{- define "tenancy-dashboard.selectorLabels" -}}
app: {{ include "tenancy-dashboard.name" . }}
{{- end -}}

{{- define "tenancy-dashboard.oauthSecretChecksum" -}}
{{- $secretName := printf "%s-oauth-client" (include "tenancy-dashboard.name" .) -}}
{{- $secret := lookup "v1" "Secret" .Values.namespace $secretName -}}
{{- if $secret }}{{ $secret.metadata.resourceVersion }}{{ else }}pending{{ end }}
{{- end -}}
