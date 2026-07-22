[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $HOME "Flawless"),
    [ValidateRange(1, 65535)]
    [int]$Port = 8080,
    [switch]$China,
    [switch]$NoStart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$RepositoryUrl = "https://github.com/your-org/Flawless.git"
$Branch = "main"

function Fail([string]$Message) {
    throw "[flawless-windows] ERROR: $Message"
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        Fail "$FilePath failed with exit code $LASTEXITCODE"
    }
}

function Get-GitValue([string[]]$Arguments) {
    $value = (& git @Arguments 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        Fail "git $($Arguments -join ' ') failed"
    }
    return $value
}

function Get-CanonicalOrigin([string]$Url) {
    $value = $Url -replace '\.git$', ''
    $value = $value -replace '^ssh://git@github\.com/', ''
    $value = $value -replace '^git@github\.com:', ''
    $value = $value -replace '^https?://github\.com/', ''
    return $value
}

function Get-DotEnvValue([string]$Path, [string]$Key, [string]$Default) {
    if (Test-Path -LiteralPath $Path) {
        $prefix = "$Key="
        foreach ($line in Get-Content -LiteralPath $Path) {
            if ($line.StartsWith($prefix)) {
                return $line.Substring($prefix.Length)
            }
        }
    }
    return $Default
}

function Convert-ToDockerHost([string]$Value) {
    return $Value.Replace("localhost", "host.docker.internal").Replace("127.0.0.1", "host.docker.internal")
}

if ($Port -in @(8100, 8101, 8102, 8103, 8105, 8200, 8300)) {
    Fail "port $Port is reserved by an internal Flawless service"
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail "Git for Windows is required: https://git-scm.com/download/win"
}

$beforeRevision = ""
$wasExisting = Test-Path -LiteralPath $InstallDir
if ($wasExisting) {
    if (-not (Test-Path -LiteralPath (Join-Path $InstallDir ".git"))) {
        Fail "$InstallDir exists but is not a Git checkout"
    }
    $origin = Get-GitValue -Arguments @("-C", $InstallDir, "remote", "get-url", "origin")
    if ((Get-CanonicalOrigin $origin) -ne "your-org/Flawless") {
        Fail "$InstallDir points to an unexpected origin: $origin"
    }
    $dirty = Get-GitValue -Arguments @("-C", $InstallDir, "status", "--porcelain")
    if ($dirty) {
        [Console]::Error.WriteLine($dirty)
        Fail "local changes detected; commit or stash them before updating"
    }
    $currentBranch = Get-GitValue -Arguments @("-C", $InstallDir, "branch", "--show-current")
    if ($currentBranch -ne $Branch) {
        Fail "$InstallDir must be on branch main (current: $currentBranch)"
    }
    $beforeRevision = Get-GitValue -Arguments @("-C", $InstallDir, "rev-parse", "HEAD")
    Write-Host "[flawless-windows] fetching the latest origin/main"
    Invoke-Checked -FilePath git -Arguments @("-C", $InstallDir, "fetch", "--prune", "origin", "main")
    Invoke-Checked -FilePath git -Arguments @("-C", $InstallDir, "merge", "--ff-only", "origin/main")
} else {
    $parent = Split-Path -Parent $InstallDir
    if ($parent) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    Write-Host "[flawless-windows] cloning main into $InstallDir"
    Invoke-Checked -FilePath git -Arguments @("clone", "--depth", "1", "--branch", "main", "--single-branch", $RepositoryUrl, $InstallDir)
}

$revision = Get-GitValue -Arguments @("-C", $InstallDir, "rev-parse", "HEAD")
$remoteRevision = Get-GitValue -Arguments @("-C", $InstallDir, "rev-parse", "origin/main")
if ($revision -ne $remoteRevision) {
    Fail "installed revision does not exactly match origin/main"
}
Write-Host "[flawless-windows] verified latest main revision: $($revision.Substring(0, 12))"

if ($NoStart) {
    Write-Host "[flawless-windows] update complete; start skipped"
    return
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail "Docker Desktop is required: https://www.docker.com/products/docker-desktop/"
}
& docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Fail "Docker Desktop is installed but not running; start it and wait until the engine is ready"
}
$composeVersion = (& docker compose version --short 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $composeVersion -notmatch '^v?2\.') {
    Fail "Docker Compose v2 is required; update Docker Desktop"
}

$envFile = Join-Path $InstallDir ".env"
if (-not (Test-Path -LiteralPath $envFile)) {
    Copy-Item -LiteralPath (Join-Path $InstallDir ".env.example") -Destination $envFile
    Write-Host "[flawless-windows] created .env from .env.example"
}

$env:FLAWLESS_PORT = $Port.ToString()
$env:FLAWLESS_DOCKER_LLM_API_BASE = Convert-ToDockerHost (Get-DotEnvValue $envFile "LLM_API_BASE" "http://localhost:11434/v1")
$env:FLAWLESS_DOCKER_EMBEDDING_API_BASE = Convert-ToDockerHost (Get-DotEnvValue $envFile "EMBEDDING_API_BASE" "http://localhost:11434/v1")
if ($China) {
    $env:FLAWLESS_NODE_IMAGE = "docker.m.daocloud.io/library/node:24-slim"
    $env:FLAWLESS_PYTHON_IMAGE = "docker.m.daocloud.io/library/python:3.13-slim"
    $env:FLAWLESS_NGINX_IMAGE = "docker.m.daocloud.io/nginxinc/nginx-unprivileged:stable-alpine3.23"
    $env:FLAWLESS_NPM_REGISTRY = "https://registry.npmmirror.com"
    $env:FLAWLESS_PIP_INDEX_URL = "https://mirrors.aliyun.com/pypi/simple"
    $env:FLAWLESS_PIP_TRUSTED_HOST = "mirrors.aliyun.com"
    $env:FLAWLESS_DEBIAN_MIRROR = "https://mirrors.aliyun.com/debian"
}

$context = (& docker context show 2>$null | Out-String).Trim()
if ($LASTEXITCODE -eq 0 -and $context) {
    & docker buildx inspect $context *> $null
    if ($LASTEXITCODE -eq 0) {
        $env:BUILDX_BUILDER = $context
    }
}

$composeArguments = @("compose", "--project-directory", $InstallDir, "-f", (Join-Path $InstallDir "compose.yaml"))
if ($wasExisting -and $beforeRevision -ne $revision) {
    Write-Host "[flawless-windows] stopping the previous revision"
    & docker @composeArguments down *> $null
}
Write-Host "[flawless-windows] building and starting Flawless with Docker"
Invoke-Checked -FilePath docker -Arguments ($composeArguments + @("up", "-d", "--build"))

$url = "http://127.0.0.1:$Port"
for ($attempt = 1; $attempt -le 90; $attempt++) {
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$url/health" -TimeoutSec 3 | Out-Null
        $health = Invoke-RestMethod -Uri "$url/api/health" -TimeoutSec 12
        if ($health.all_healthy -eq $true) {
            Write-Host "[flawless-windows] ready: $url"
            return
        }
    } catch {}
    Start-Sleep -Seconds 1
}

& docker @composeArguments logs --tail 120
Fail "Docker stack did not become healthy within 90 seconds"
