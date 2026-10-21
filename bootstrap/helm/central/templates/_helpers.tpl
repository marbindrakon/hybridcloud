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

{{/*
A ManagedServiceAccount on an ACM managed cluster with its ClusterRole and a kubeconfig in central Vault.
On central (the ACM hub), in the managed cluster's namespace:
  ManagedServiceAccount  ACM creates the service account (in open-cluster-management-agent-addon on the cluster)
                         and keeps a rotated token here
  ClusterPermission      the account's ClusterRole on the cluster (rules) and its binding
  PushSecret             a kubeconfig (cluster API + token) to central Vault (vaultKey, property kubeconfig),
                         re-pushed when ACM rotates the token; deletionPolicy Delete removes the Vault key with it
The cluster API serves a publicly trusted certificate, so the kubeconfig carries no CA.
Parameters (dict): root, cluster, sa, wave, tokenValidity, rules, server, vaultKey, deletionPolicy.
*/}}
{{- define "sovereign-central.managedAccount" -}}
---
apiVersion: authentication.open-cluster-management.io/v1beta1
kind: ManagedServiceAccount
metadata:
  name: {{ .sa }}
  namespace: {{ .cluster }}
  labels:
    {{- include "sovereign-central.labels" .root | nindent 4 }}
    sovereign-cloud/target: {{ .cluster }}
  annotations:
    argocd.argoproj.io/sync-wave: {{ .wave | quote }}
spec:
  rotation:
    enabled: true
    validity: {{ .tokenValidity | default "168h" }}
---
apiVersion: rbac.open-cluster-management.io/v1alpha1
kind: ClusterPermission
metadata:
  name: {{ .sa }}
  namespace: {{ .cluster }}
  labels:
    {{- include "sovereign-central.labels" .root | nindent 4 }}
    sovereign-cloud/target: {{ .cluster }}
  annotations:
    argocd.argoproj.io/sync-wave: {{ .wave | quote }}
spec:
  clusterRole:
    rules:
      {{- toYaml .rules | nindent 6 }}
  clusterRoleBinding:
    subject:
      kind: ServiceAccount
      name: {{ .sa }}
      namespace: open-cluster-management-agent-addon
---
apiVersion: external-secrets.io/v1alpha1
kind: PushSecret
metadata:
  name: {{ .sa }}
  namespace: {{ .cluster }}
  labels:
    {{- include "sovereign-central.labels" .root | nindent 4 }}
    sovereign-cloud/target: {{ .cluster }}
  annotations:
    argocd.argoproj.io/sync-wave: {{ .wave | quote }}
spec:
  refreshInterval: 1h
  updatePolicy: Replace
  deletionPolicy: {{ .deletionPolicy | default "Delete" }}
  secretStoreRefs:
    - kind: ClusterSecretStore
      name: vault-backend
  selector:
    secret:
      name: {{ .sa }}
  template:
    engineVersion: v2
    data:
      kubeconfig: |
        apiVersion: v1
        kind: Config
        clusters:
          - name: {{ .cluster }}
            cluster:
              server: {{ .server }}
        users:
          - name: {{ .sa }}
            user:
              token: {{ "{{ .token }}" }}
        contexts:
          - name: {{ .cluster }}
            context:
              cluster: {{ .cluster }}
              user: {{ .sa }}
        current-context: {{ .cluster }}
  data:
    - conversionStrategy: None
      match:
        secretKey: kubeconfig
        remoteRef:
          remoteKey: {{ .vaultKey }}
          property: kubeconfig
{{- end }}
