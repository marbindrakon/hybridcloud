{{/* "true" when the operator's own Ingress serves the Keycloak (keycloak.ingress.enabled, default true). */}}
{{- define "rhbk.ingressEnabled" -}}
{{- $i := .Values.keycloak.ingress | default dict -}}
{{- if or (not (hasKey $i "enabled")) $i.enabled -}}true{{- end -}}
{{- end -}}

{{/* Name of route number .i of keycloak.routes (.r the entry, .root the chart context). */}}
{{- define "rhbk.routeName" -}}
{{- .r.name | default (printf "%s-route-%d" .root.Values.instanceName .i) -}}
{{- end -}}
