[CmdletBinding()]
param(
    [ValidateSet("amd64", "arm64")]
    [string]$Platform = "amd64",

    [string]$PrivateRegistry = "",

    [switch]$IncludeObservability,
    [switch]$IncludeEbpf,
    [switch]$IncludeLangfuse,
    [switch]$SkipRegistryLogin
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Fail([string]$Message) {
    throw "[k8s-agent-images] ERROR: $Message"
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    Write-Host "> $FilePath $($Arguments -join ' ')"
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        Fail "$FilePath failed with exit code $LASTEXITCODE"
    }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail "Docker Desktop or Docker CLI is required"
}
& docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Fail "Docker is installed but the Docker engine is not running"
}

$images = @(
    @{
        Name = "Kubernetes Agent"
        Source = "m.daocloud.io/ghcr.io/your-org/flawless:3.2.2"
        Target = "k8s-agent:3.2.2"
    },
    @{
        Name = "Approved Node Executor"
        Source = "m.daocloud.io/ghcr.io/your-org/flawless-node-exec:1.36"
        Target = "k8s-agent-node-exec:1.36"
    }
)

if ($IncludeObservability) {
    $images += @(
        @{
            Name = "Prometheus"
            Source = "registry.cn-hangzhou.aliyuncs.com/google_containers/prometheus:v2.45.0"
            Target = "k8s-agent-prometheus:v2.45.0"
        },
        @{
            Name = "kube-state-metrics"
            Source = "m.daocloud.io/registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.10.1"
            Target = "k8s-agent-kube-state-metrics:v2.10.1"
        },
        @{
            Name = "Loki"
            Source = "m.daocloud.io/docker.io/grafana/loki:3.7.3"
            Target = "k8s-agent-loki:3.7.3"
        },
        @{
            Name = "Tempo"
            Source = "m.daocloud.io/docker.io/grafana/tempo:2.10.5"
            Target = "k8s-agent-tempo:2.10.5"
        },
        @{
            Name = "Alloy"
            Source = "m.daocloud.io/docker.io/grafana/alloy:v1.16.1"
            Target = "k8s-agent-alloy:v1.16.1"
        },
        @{
            Name = "Grafana"
            Source = "m.daocloud.io/docker.io/grafana/grafana:13.0.2"
            Target = "k8s-agent-grafana:13.0.2"
        }
    )
}

if ($IncludeEbpf) {
    $images += @{
        Name = "Beyla eBPF"
        Source = "m.daocloud.io/docker.io/grafana/beyla:3.24.0"
        Target = "k8s-agent-beyla:3.24.0"
    }
}

if ($IncludeLangfuse) {
    Write-Warning "The bundled Langfuse manifest is optional and must be security-reviewed before production use."
    $images += @(
        @{
            Name = "PostgreSQL for Langfuse"
            Source = "m.daocloud.io/docker.io/library/postgres:16-alpine"
            Target = "k8s-agent-postgres:16-alpine"
        },
        @{
            Name = "Langfuse Web"
            Source = "m.daocloud.io/docker.io/langfuse/langfuse:latest"
            Target = "k8s-agent-langfuse:latest"
        },
        @{
            Name = "Langfuse Worker"
            Source = "m.daocloud.io/docker.io/langfuse/langfuse-worker:latest"
            Target = "k8s-agent-langfuse-worker:latest"
        }
    )
}

$registry = $PrivateRegistry.Trim().TrimEnd('/')
if ($registry -match '://') {
    Fail "PrivateRegistry must be host[:port] without http:// or https://"
}
if ($registry -and -not $SkipRegistryLogin) {
    Invoke-Checked -FilePath "docker" -Arguments @("login", $registry)
}

Write-Host "[k8s-agent-images] Pull order: application, node executor, then optional dependencies"
foreach ($image in $images) {
    Write-Host ""
    Write-Host "[k8s-agent-images] Pulling $($image.Name)"
    Invoke-Checked -FilePath "docker" -Arguments @(
        "pull", "--platform", "linux/$Platform", $image.Source
    )

    if ($registry) {
        $target = "$registry/$($image.Target)"
        Invoke-Checked -FilePath "docker" -Arguments @("tag", $image.Source, $target)
        Invoke-Checked -FilePath "docker" -Arguments @("push", $target)
        Write-Host "[k8s-agent-images] Published: $target"
    }
}

Write-Host ""
if ($registry) {
    Write-Host "[k8s-agent-images] All selected images were pushed to $registry"
} else {
    Write-Host "[k8s-agent-images] All selected public images were pulled locally"
}
