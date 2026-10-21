{{- define "primary-operator.name" -}}
hybridsovereign-primary-operator
{{- end }}

{{- define "primary-operator.labels" -}}
app.kubernetes.io/name: {{ include "primary-operator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: hybridsovereign
{{- end }}

{{- define "primary-operator.selectorLabels" -}}
app.kubernetes.io/name: {{ include "primary-operator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Admission policy helpers (shared by templates/admission-policies.yaml and templates/gitops-policies.yaml).
*/}}
{{- define "primary-operator.celStringList" -}}
[{{- range $i, $v := . }}{{ if $i }}, {{ end }}{{ $v | toJson }}{{- end }}]
{{- end }}
{{- define "primary-operator.vapActions" -}}
{{- if eq . "Deny" }}[Deny, Audit]{{ else if eq . "Warn" }}[Warn, Audit]{{ else }}{{ fail (printf "admission mode must be Warn or Deny, got %q" .) }}{{ end }}
{{- end }}
{{- define "primary-operator.vapFailurePolicy" -}}
{{- if eq . "Deny" }}Fail{{ else }}Ignore{{ end }}
{{- end }}
{{- define "primary-operator.isAutomationCEL" -}}
(request.userInfo.username in {{ include "primary-operator.celStringList" .Values.admission.automationUsernames }})
{{- end }}
{{- define "primary-operator.isGitopsCEL" -}}
(request.userInfo.username in {{ include "primary-operator.celStringList" .Values.admission.gitopsUsernames }})
{{- end }}
{{- define "primary-operator.isNamespaceOperatorCEL" -}}
(request.userInfo.username.startsWith('system:serviceaccount:entity-') && request.userInfo.username.endsWith({{ printf ":%s" .Values.admission.namespaceOperatorServiceAccount | toJson }}))
{{- end }}
{{- define "primary-operator.isPlatformGroupCEL" -}}
(request.userInfo.groups.exists(g, g in {{ include "primary-operator.celStringList" .Values.admission.platformGroups }}))
{{- end }}
