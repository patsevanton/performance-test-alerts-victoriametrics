{{- define "golden-signal-app.name" -}}
{{ .Release.Name }}
{{- end }}

{{- define "golden-signal-app.labels" -}}
app: {{ .Release.Name }}
app.kubernetes.io/name: {{ .Release.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "golden-signal-app.selectorLabels" -}}
app: {{ .Release.Name }}
app.kubernetes.io/name: {{ .Release.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
