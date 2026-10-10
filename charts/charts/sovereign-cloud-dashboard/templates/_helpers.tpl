{{- define "dashboard.name" -}}
sovereign-cloud-dashboard
{{- end -}}

{{- define "dashboard.labels" -}}
app.kubernetes.io/name: {{ include "dashboard.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: hybrid-sovereign-cloud
{{- end -}}

{{- define "dashboard.selectorLabels" -}}
app: {{ include "dashboard.name" . }}
{{- end -}}

{{- define "dashboard.oauthSecretChecksum" -}}
{{- $secretName := printf "%s-oauth-client" (include "dashboard.name" .) -}}
{{- $secret := lookup "v1" "Secret" .Values.namespace $secretName -}}
{{- if $secret }}{{ $secret.metadata.resourceVersion }}{{ else }}pending{{ end }}
{{- end -}}
