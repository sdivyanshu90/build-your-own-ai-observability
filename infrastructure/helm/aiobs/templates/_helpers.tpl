{{/*
Naming and label helpers.

Names are truncated to 63 characters because that is the DNS label limit, and a
release name long enough to overflow it produces objects Kubernetes silently
refuses to create.
*/}}

{{- define "aiobs.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "aiobs.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "aiobs.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "aiobs.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: aiobs
{{- with .Values.extraLabels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "aiobs.selectorLabels" -}}
app.kubernetes.io/name: {{ include "aiobs.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "aiobs.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "aiobs.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Fully-qualified image reference.

A digest wins over a tag: with a mutable tag, "roll back to the previous
version" is not a well-defined operation.
*/}}
{{- define "aiobs.image" -}}
{{- $tag := default .Chart.AppVersion .Values.image.tag -}}
{{- if .Values.image.digest -}}
{{- printf "%s/%s/%s@%s" .Values.image.registry .Values.image.repository .component .Values.image.digest -}}
{{- else -}}
{{- printf "%s/%s/%s:%s" .Values.image.registry .Values.image.repository .component $tag -}}
{{- end -}}
{{- end -}}

{{/*
Environment shared by the API and the worker.

Secrets are referenced, never interpolated: a rendered manifest must be safe to
commit to a GitOps repository.
*/}}
{{- define "aiobs.env" -}}
- name: AIOBS_ENVIRONMENT
  value: {{ .Values.config.environment | quote }}
- name: AIOBS_LOG_LEVEL
  value: {{ .Values.config.logLevel | quote }}
- name: AIOBS_LOG_FORMAT
  value: {{ .Values.config.logFormat | quote }}
- name: AIOBS_PUBLIC_URL
  value: {{ required "config.publicUrl is required" .Values.config.publicUrl | quote }}
- name: AIOBS_WEB_URL
  value: {{ default .Values.config.publicUrl .Values.config.webUrl | quote }}
- name: AIOBS_DATABASE__URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secrets.existingSecret | quote }}
      key: {{ .Values.secrets.keys.databaseUrl | quote }}
- name: AIOBS_ANALYTICS__DRIVER
  value: {{ .Values.config.analytics.driver | quote }}
- name: AIOBS_ANALYTICS__URL
  value: {{ required "config.analytics.url is required" .Values.config.analytics.url | quote }}
- name: AIOBS_ANALYTICS__DATABASE
  value: {{ .Values.config.analytics.database | quote }}
{{- if .Values.secrets.keys.analyticsPassword }}
- name: AIOBS_ANALYTICS__PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secrets.existingSecret | quote }}
      key: {{ .Values.secrets.keys.analyticsPassword | quote }}
{{- end }}
- name: AIOBS_KV__DRIVER
  value: {{ .Values.config.kv.driver | quote }}
- name: AIOBS_KV__URL
  value: {{ required "config.kv.url is required" .Values.config.kv.url | quote }}
- name: AIOBS_BUS__DRIVER
  value: {{ .Values.config.bus.driver | quote }}
- name: AIOBS_BUS__BROKERS
  value: {{ required "config.bus.brokers is required" .Values.config.bus.brokers | quote }}
- name: AIOBS_BUS__TOPIC
  value: {{ .Values.config.bus.topic | quote }}
- name: AIOBS_BUS__CONSUMER_GROUP
  value: {{ .Values.config.bus.consumerGroup | quote }}
- name: AIOBS_OBJECTS__DRIVER
  value: {{ .Values.config.objects.driver | quote }}
- name: AIOBS_OBJECTS__BUCKET
  value: {{ required "config.objects.bucket is required" .Values.config.objects.bucket | quote }}
{{- with .Values.config.objects.region }}
- name: AIOBS_OBJECTS__REGION
  value: {{ . | quote }}
{{- end }}
{{- with .Values.config.objects.endpointUrl }}
- name: AIOBS_OBJECTS__ENDPOINT_URL
  value: {{ . | quote }}
{{- end }}
{{- if .Values.secrets.keys.objectsAccessKeyId }}
- name: AIOBS_OBJECTS__ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secrets.existingSecret | quote }}
      key: {{ .Values.secrets.keys.objectsAccessKeyId | quote }}
- name: AIOBS_OBJECTS__SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secrets.existingSecret | quote }}
      key: {{ .Values.secrets.keys.objectsSecretAccessKey | quote }}
{{- end }}
- name: AIOBS_AUTH__JWT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secrets.existingSecret | quote }}
      key: {{ .Values.secrets.keys.jwtSecret | quote }}
- name: AIOBS_AUTH__API_KEY_PEPPER
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secrets.existingSecret | quote }}
      key: {{ .Values.secrets.keys.apiKeyPepper | quote }}
- name: AIOBS_SECURITY__CURSOR_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secrets.existingSecret | quote }}
      key: {{ .Values.secrets.keys.cursorSecret | quote }}
- name: AIOBS_SECURITY__CORS_ALLOW_ORIGINS
  value: {{ toJson (required "config.security.corsAllowOrigins is required" .Values.config.security.corsAllowOrigins) | quote }}
- name: AIOBS_SECURITY__TRUSTED_PROXY_HOPS
  value: {{ .Values.config.security.trustedProxyHops | quote }}
- name: AIOBS_INGEST__MAX_BATCH_SPANS
  value: {{ .Values.config.ingest.maxBatchSpans | quote }}
- name: AIOBS_SECURITY__MAX_REQUEST_BYTES
  value: {{ .Values.config.ingest.maxRequestBytes | quote }}
- name: AIOBS_INGEST__ALLOW_ANONYMOUS_INGEST
  value: {{ .Values.config.ingest.allowAnonymous | quote }}
{{- with .Values.telemetry.otlpEndpoint }}
- name: AIOBS_TELEMETRY__OTLP_ENDPOINT
  value: {{ . | quote }}
{{- end }}
- name: POD_NAME
  valueFrom:
    fieldRef:
      fieldPath: metadata.name
- name: POD_NAMESPACE
  valueFrom:
    fieldRef:
      fieldPath: metadata.namespace
{{- with .Values.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/*
A read-only root filesystem needs somewhere to write. These are the only paths
the processes touch.
*/}}
{{- define "aiobs.writableVolumes" -}}
- name: tmp
  emptyDir: {}
{{- end -}}

{{- define "aiobs.writableVolumeMounts" -}}
- name: tmp
  mountPath: /tmp
{{- end -}}
