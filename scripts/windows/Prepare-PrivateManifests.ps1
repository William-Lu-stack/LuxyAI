[CmdletBinding()]
param(
    [string]$PrivateRegistry = "registry.example.com",
    [string]$OutputDirectory = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$registry = $PrivateRegistry.Trim().TrimEnd('/')
if (-not $registry -or $registry -match '://') {
    throw "PrivateRegistry must be host[:port] without http:// or https://"
}

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $repositoryRoot "generated-private-manifests"
}
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

$replacements = [ordered]@{
    "m.daocloud.io/ghcr.io/your-org/flawless:3.2.2" = "$registry/k8s-agent:3.2.2"
    "m.daocloud.io/ghcr.io/your-org/flawless-node-exec:1.36" = "$registry/k8s-agent-node-exec:1.36"
    "registry.cn-hangzhou.aliyuncs.com/google_containers/prometheus:v2.45.0" = "$registry/k8s-agent-prometheus:v2.45.0"
    "m.daocloud.io/registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.10.1" = "$registry/k8s-agent-kube-state-metrics:v2.10.1"
    "m.daocloud.io/docker.io/grafana/loki:3.7.3" = "$registry/k8s-agent-loki:3.7.3"
    "m.daocloud.io/docker.io/grafana/tempo:2.10.5" = "$registry/k8s-agent-tempo:2.10.5"
    "m.daocloud.io/docker.io/grafana/alloy:v1.16.1" = "$registry/k8s-agent-alloy:v1.16.1"
    "m.daocloud.io/docker.io/grafana/grafana:13.0.2" = "$registry/k8s-agent-grafana:13.0.2"
    "m.daocloud.io/docker.io/grafana/beyla:3.24.0" = "$registry/k8s-agent-beyla:3.24.0"
}

$manifests = @(
    @{ Source = "manifests\observability-stack.yaml"; Target = "30-observability-stack.yaml" },
    @{ Source = "manifests\grafana-observability.yaml"; Target = "40-grafana-observability.yaml" },
    @{ Source = "manifests\ebpf-beyla.yaml"; Target = "50-ebpf-beyla.yaml" }
)

foreach ($manifest in $manifests) {
    $sourcePath = Join-Path $repositoryRoot $manifest.Source
    $targetPath = Join-Path $OutputDirectory $manifest.Target
    $content = Get-Content -LiteralPath $sourcePath -Raw
    foreach ($sourceImage in $replacements.Keys) {
        $content = $content.Replace($sourceImage, $replacements[$sourceImage])
    }
    Set-Content -LiteralPath $targetPath -Value $content -Encoding utf8
    Write-Host "[k8s-agent-manifests] Generated: $targetPath"
}

Write-Host ""
Write-Host "Apply only the components you need, in numeric order:"
Write-Host "  kubectl apply -f $OutputDirectory\30-observability-stack.yaml"
Write-Host "  kubectl apply -f $OutputDirectory\40-grafana-observability.yaml"
Write-Host "  kubectl apply -f $OutputDirectory\50-ebpf-beyla.yaml"
