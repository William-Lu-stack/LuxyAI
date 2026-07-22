[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$StorageClass,

    [ValidateSet("public-cn", "private")]
    [string]$ImageMode = "public-cn",

    [string]$PrivateRegistry = "registry.example.com",
    [string]$Namespace = "k8s-agent",
    [string]$ReleaseName = "k8s-agent",
    [string]$LlmApiBase = "https://api.deepseek.com/v1",
    [string]$LlmModel = "deepseek-reasoner",
    [string]$RegistryUsername = "",
    [switch]$SkipSecrets,
    [switch]$SkipRegistryLogin,
    [switch]$SkipNodeExecutor,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Fail([string]$Message) {
    throw "[k8s-agent-deploy] ERROR: $Message"
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

function Convert-ToPlainText([Security.SecureString]$SecureValue) {
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureValue)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

function Apply-GeneratedSecret([string[]]$CreateArguments) {
    $yaml = & kubectl @CreateArguments
    if ($LASTEXITCODE -ne 0) {
        Fail "kubectl failed to generate a Secret"
    }
    $yaml | & kubectl apply -f -
    if ($LASTEXITCODE -ne 0) {
        Fail "kubectl failed to apply a Secret"
    }
}

foreach ($command in @("kubectl", "helm")) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        Fail "$command is required and must be available in PATH"
    }
}

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$chartPath = Join-Path $repositoryRoot "charts\flawless"
$nodeExecutorPath = Join-Path $repositoryRoot "manifests\node-executor.yaml"
if (-not (Test-Path -LiteralPath $chartPath)) {
    Fail "Helm chart not found at $chartPath"
}

$context = (& kubectl config current-context 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $context) {
    Fail "kubectl has no active Kubernetes context"
}
Write-Host "[k8s-agent-deploy] Target context: $context"
Write-Host "[k8s-agent-deploy] Namespace: $Namespace"
Write-Host "[k8s-agent-deploy] StorageClass: $StorageClass (must support ReadWriteMany)"
if (-not $Force) {
    $confirmation = Read-Host "Type DEPLOY to continue"
    if ($confirmation -cne "DEPLOY") {
        Fail "deployment cancelled"
    }
}

$namespaceYaml = & kubectl create namespace $Namespace --dry-run=client -o yaml
if ($LASTEXITCODE -ne 0) {
    Fail "failed to generate namespace manifest"
}
$namespaceYaml | & kubectl apply -f -
if ($LASTEXITCODE -ne 0) {
    Fail "failed to create namespace $Namespace"
}

if ($ImageMode -eq "public-cn") {
    $appRepository = "m.daocloud.io/ghcr.io/your-org/flawless"
    $appTag = "3.2.2"
    $nodeExecutorImage = "m.daocloud.io/ghcr.io/your-org/flawless-node-exec:1.36"
} else {
    $registry = $PrivateRegistry.Trim().TrimEnd('/')
    if (-not $registry -or $registry -match '://') {
        Fail "PrivateRegistry must be host[:port] without a URL scheme"
    }
    $appRepository = "$registry/k8s-agent"
    $appTag = "3.2.2"
    $nodeExecutorImage = "$registry/k8s-agent-node-exec:1.36"

    if ($RegistryUsername) {
        $registryPasswordSecure = Read-Host "Private registry password" -AsSecureString
        $registryPassword = Convert-ToPlainText $registryPasswordSecure
        try {
            if (-not $SkipRegistryLogin) {
                $registryPassword | & docker login $registry --username $RegistryUsername --password-stdin
                if ($LASTEXITCODE -ne 0) {
                    Fail "docker login failed"
                }
            }
            Apply-GeneratedSecret -CreateArguments @(
                "-n", $Namespace, "create", "secret", "docker-registry", "regcred",
                "--docker-server=$registry", "--docker-username=$RegistryUsername",
                "--docker-password=$registryPassword", "--dry-run=client", "-o", "yaml"
            )
        } finally {
            $registryPassword = $null
        }
    }
}

if (-not $SkipSecrets) {
    $apiKeySecure = Read-Host "OpenAI-compatible / DeepSeek API key" -AsSecureString
    $apiKey = Convert-ToPlainText $apiKeySecure
    try {
        if (-not $apiKey) {
            Fail "LLM API key cannot be empty"
        }
        Apply-GeneratedSecret -CreateArguments @(
            "-n", $Namespace, "create", "secret", "generic", "k8s-agent-oauth",
            "--from-literal=LLM_API_KEY=$apiKey", "--dry-run=client", "-o", "yaml"
        )
    } finally {
        $apiKey = $null
    }

    & kubectl -n $Namespace get secret flawless-cluster-credentials *> $null
    if ($LASTEXITCODE -ne 0) {
        $keyBytes = New-Object byte[] 32
        $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
        try {
            $generator.GetBytes($keyBytes)
        } finally {
            $generator.Dispose()
        }
        $fernetKey = [Convert]::ToBase64String($keyBytes).Replace('+', '-').Replace('/', '_')
        Apply-GeneratedSecret -CreateArguments @(
            "-n", $Namespace, "create", "secret", "generic", "flawless-cluster-credentials",
            "--from-literal=fernet-key=$fernetKey", "--dry-run=client", "-o", "yaml"
        )
        $fernetKey = $null
    } else {
        Write-Host "[k8s-agent-deploy] Keeping the existing cluster credential encryption key"
    }
}

$helmArguments = @(
    "upgrade", "--install", $ReleaseName, $chartPath,
    "--namespace", $Namespace, "--create-namespace",
    "--set-string", "image.repository=$appRepository",
    "--set-string", "image.tag=$appTag",
    "--set", "image.pullPolicy=IfNotPresent",
    "--set", "serviceAccount.name=k8s-agent-sa",
    "--set-string", "persistence.storageClass=$StorageClass",
    "--set-string", "config.LLM_API_BASE=$LlmApiBase",
    "--set-string", "config.LLM_MODEL=$LlmModel",
    "--set-string", "config.LLM_AUTH_TYPE=api_key",
    "--set-string", "config.LLM_VERIFY_SSL=true",
    "--set-string", "config.OPS_MUTATION_ENABLED=true",
    "--set-string", "config.AUTONOMOUS_OPS_ENABLED=false",
    "--set-string", "config.OPS_STEPWISE_CONFIRMATION_REQUIRED=true",
    "--set-string", "config.SKILL_EXECUTION_REQUIRED=true",
    "--set-string", "config.NODE_EXEC_IMAGE=$nodeExecutorImage",
    "--set-string", "config.KNOWLEDGE_EMBEDDING_ENABLED=false",
    "--set-string", "config.PROMETHEUS_URL=",
    "--set-string", "config.CMDB_URL=",
    "--set-string", "config.LANGFUSE_HOST="
)
if ($ImageMode -eq "private" -and $RegistryUsername) {
    $helmArguments += @("--set", "imagePullSecrets[0].name=regcred")
}
Invoke-Checked -FilePath "helm" -Arguments $helmArguments

if (-not $SkipNodeExecutor) {
    Invoke-Checked -FilePath "kubectl" -Arguments @("apply", "-f", $nodeExecutorPath)
}

Invoke-Checked -FilePath "kubectl" -Arguments @(
    "-n", $Namespace, "rollout", "status", "deployment/$ReleaseName-flawless-api", "--timeout=5m"
)
Invoke-Checked -FilePath "kubectl" -Arguments @(
    "-n", $Namespace, "rollout", "status", "deployment/$ReleaseName-flawless-agents", "--timeout=10m"
)
Invoke-Checked -FilePath "kubectl" -Arguments @("-n", $Namespace, "get", "pods", "-o", "wide")

Write-Host ""
Write-Host "[k8s-agent-deploy] Deployment completed"
Write-Host "[k8s-agent-deploy] Open: http://<any-node-ip>:30080"
