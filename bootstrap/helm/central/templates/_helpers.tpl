{{/*
Chart name
*/}}
{{- define "sovereign-central.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "sovereign-central.labels" -}}
helm.sh/chart: {{ include "sovereign-central.name" . }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ include "sovereign-central.name" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: sovereign-bootstrap
{{- end }}

{{/*
OCI chart URL helper: oci://<registry>/<repositoryBase>/rhacm
ArgoCD v3.x resolves against the full repo path.
*/}}
{{- define "sovereign-central.ociURL" -}}
{{- printf "oci://%s/%s/rhacm" .Values.oci.registry .Values.oci.repositoryBase }}
{{- end }}

{{/*
Generic OCI base URL helper: oci://<registry>/<repositoryBase>
*/}}
{{- define "sovereign-central.ociBase" -}}
{{- printf "oci://%s/%s" .Values.oci.registry .Values.oci.repositoryBase }}
{{- end }}

{{/*
True when .Values.applicationsEnabled allows rendering Application resources.
*/}}
{{- define "sovereign-central.applicationsEnabled" -}}
{{- if .Values.applicationsEnabled }}true{{- end }}
{{- end }}

{{/*
Automated sync retry — used by child Applications after a failed/timed-out sync.
*/}}
{{- define "sovereign-central.syncRetry" -}}
retry:
  limit: {{ .Values.argocd.syncPolicy.retry.limit }}
  backoff:
    duration: {{ .Values.argocd.syncPolicy.retry.backoff.duration }}
    factor: {{ .Values.argocd.syncPolicy.retry.backoff.factor }}
    maxDuration: {{ .Values.argocd.syncPolicy.retry.backoff.maxDuration }}
{{- end }}

{{/*
Ignore ESO controller defaults on ExternalSecret/PushSecret (conversionStrategy, etc.; shc: the Red Hat
ESO 1.x operator also defaults remoteRef.nullBytePolicy and target.template.mergePolicy).
*/}}
{{- define "sovereign-central.esoIgnoreDifferences" -}}
- group: external-secrets.io
  kind: ExternalSecret
  jqPathExpressions:
    - .spec.data[].remoteRef.conversionStrategy
    - .spec.data[].remoteRef.decodingStrategy
    - .spec.data[].remoteRef.metadataPolicy
    - .spec.data[].remoteRef.nullBytePolicy
    - .spec.target.deletionPolicy
    - .spec.target.template.mergePolicy
    - .spec.refreshInterval
  jsonPointers:
    - /spec/dataFrom
    - /metadata/annotations
- group: external-secrets.io
  kind: PushSecret
  jqPathExpressions:
    - .spec.data[].conversionStrategy
    - .spec.data[].decodingStrategy
    - .spec.data[].metadataPolicy
    - .spec.deletionPolicy
    - .spec.updatePolicy
    - .spec.refreshInterval
  jsonPointers:
    - /metadata/annotations
    - /spec/selector
    - /status
{{- end }}
