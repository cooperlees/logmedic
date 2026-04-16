{{/*
Expand the name of the chart.
*/}}
{{- define "logmedic.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "logmedic.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "logmedic.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "logmedic.labels" -}}
helm.sh/chart: {{ include "logmedic.chart" . }}
{{ include "logmedic.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "logmedic.selectorLabels" -}}
app.kubernetes.io/name: {{ include "logmedic.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use.
*/}}
{{- define "logmedic.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "logmedic.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Resolve secret name for API tokens.
*/}}
{{- define "logmedic.secretName" -}}
{{- if .Values.secrets.name -}}
{{- .Values.secrets.name -}}
{{- else if .Values.secrets.create -}}
{{- include "logmedic.fullname" . -}}
{{- else -}}
{{- "" -}}
{{- end -}}
{{- end }}
