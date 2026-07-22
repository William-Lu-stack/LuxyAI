{{- define "flawless.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "flawless.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "flawless.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "flawless.namespace" -}}
{{- default .Release.Namespace .Values.namespaceOverride -}}
{{- end -}}

{{- define "flawless.labels" -}}
app.kubernetes.io/name: {{ include "flawless.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "flawless.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "flawless.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "flawless.image" -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) -}}
{{- end -}}

{{- define "flawless.runtimeClaim" -}}
{{- default (printf "%s-runtime" (include "flawless.fullname" .)) .Values.persistence.existingClaim -}}
{{- end -}}

{{- define "flawless.envFrom" -}}
- configMapRef:
    name: {{ include "flawless.fullname" . }}-config
- secretRef:
    name: {{ .Values.secrets.oauth }}
    optional: true
{{- end -}}

{{- define "flawless.volumeMounts" -}}
- name: runtime-store
  mountPath: /var/lib/flawless
- name: runtime-store
  mountPath: /var/lib/flawless
- name: private-algorithms
  mountPath: /var/lib/flawless-custom
  readOnly: true
- name: runtime-tmp
  mountPath: /tmp
{{- end -}}

{{- define "flawless.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}

{{- define "flawless.volumePermissionInitContainers" -}}
{{- if and .Values.persistence.enabled .Values.persistence.volumePermissions.enabled }}
initContainers:
  - name: runtime-store-permissions
    image: {{ include "flawless.image" . | quote }}
    imagePullPolicy: {{ .Values.image.pullPolicy }}
    command: ["sh", "-ec"]
    args:
      - |
        chown -R {{ .Values.persistence.volumePermissions.runAsUser }}:{{ .Values.persistence.volumePermissions.runAsGroup }} /var/lib/flawless || \
          echo "WARNING: storage backend rejected chown; the application will verify write access"
        chmod -R u+rwX,g+rwX /var/lib/flawless || \
          echo "WARNING: storage backend rejected chmod; the application will verify write access"
    securityContext:
      runAsNonRoot: false
      runAsUser: 0
      runAsGroup: 0
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities:
        drop: ["ALL"]
    volumeMounts:
      - name: runtime-store
        mountPath: /var/lib/flawless
{{- end -}}
{{- end -}}
