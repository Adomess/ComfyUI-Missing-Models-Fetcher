param(
    [Parameter(Mandatory = $true)]
    [string]$ComfyCli,

    [string]$SourceRepo = "",
    [string]$ComfyUISource = "C:\ComfyUI\ComfyUI",
    [string]$Python = "py",
    [switch]$KeepTemp
)

$ErrorActionPreference = "Stop"

if (-not $SourceRepo) {
    $SourceRepo = Split-Path -Parent $PSScriptRoot
}

function Invoke-RobocopyChecked {
    param(
        [string]$Source,
        [string]$Destination,
        [string[]]$ExtraArgs
    )

    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    & robocopy $Source $Destination /E /NFL /NDL /NJH /NJS /NP @ExtraArgs | Out-Null
    if ($LASTEXITCODE -ge 8) {
        throw "robocopy failed with exit code ${LASTEXITCODE}: $Source -> $Destination"
    }
}

function Invoke-ComfyNode {
    param([string[]]$Arguments)
    & $ComfyCli --workspace $script:TestWorkspace --skip-prompt node @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Comfy CLI node command failed: $($Arguments -join ' ')"
    }
}

if (-not (Test-Path -LiteralPath $ComfyCli -PathType Leaf)) {
    throw "Comfy CLI does not exist: $ComfyCli"
}
if (-not (Test-Path -LiteralPath $SourceRepo -PathType Container)) {
    throw "Source repo does not exist: $SourceRepo"
}
if (-not (Test-Path -LiteralPath $ComfyUISource -PathType Container)) {
    throw "ComfyUI source does not exist: $ComfyUISource"
}

$managerSource = Join-Path $ComfyUISource "custom_nodes\comfyui-manager"
if (-not (Test-Path -LiteralPath $managerSource -PathType Container)) {
    throw "ComfyUI-Manager source does not exist: $managerSource"
}
$managerCliSource = Join-Path (Split-Path -Parent $ComfyUISource) "python_embeded\Lib\site-packages\cm_cli"
if (-not (Test-Path -LiteralPath $managerCliSource -PathType Container)) {
    throw "ComfyUI-Manager CLI bootstrap module does not exist: $managerCliSource"
}
$managerPackageSource = Join-Path (Split-Path -Parent $ComfyUISource) "python_embeded\Lib\site-packages\comfyui_manager"
if (-not (Test-Path -LiteralPath $managerPackageSource -PathType Container)) {
    throw "ComfyUI-Manager Python package does not exist: $managerPackageSource"
}
$comfyCliVenv = Split-Path -Parent (Split-Path -Parent $ComfyCli)
$comfyCliPython = Join-Path $comfyCliVenv "Scripts\python.exe"
foreach ($bootstrapPackage in @("aiohttp", "tqdm", "toml", "huggingface_hub", "chardet")) {
    $savedErrorPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $comfyCliPython -c "import $bootstrapPackage" 2>$null
    $bootstrapImportExitCode = $LASTEXITCODE
    $ErrorActionPreference = $savedErrorPreference
    if ($bootstrapImportExitCode -ne 0) {
        & $comfyCliPython -m pip install --disable-pip-version-check $bootstrapPackage
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to install $bootstrapPackage into the disposable Comfy CLI environment"
        }
    }
}
$managerCliDestination = Join-Path $comfyCliVenv "Lib\site-packages\cm_cli"
Invoke-RobocopyChecked -Source $managerCliSource -Destination $managerCliDestination -ExtraArgs @(
    "/XD", "__pycache__",
    "/XF", "*.pyc", "*.pyo"
)
$managerPackageDestination = Join-Path $comfyCliVenv "Lib\site-packages\comfyui_manager"
Invoke-RobocopyChecked -Source $managerPackageSource -Destination $managerPackageDestination -ExtraArgs @(
    "/XD", "__pycache__",
    "/XF", "*.pyc", "*.pyo", "*.log"
)

$tempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\')
$testRoot = Join-Path $tempBase ("mmf-manager-lifecycle-" + [guid]::NewGuid().ToString("N"))
$script:TestWorkspace = Join-Path $testRoot "ComfyUI"
$sourceCopy = Join-Path $testRoot "source"
$httpRoot = Join-Path $testRoot "http"
$bareRepo = Join-Path $httpRoot "mmf.git"
$server = $null

$report = [ordered]@{
    ok = $false
    install = $false
    disable = $false
    enable = $false
    update = $false
    uninstall = $false
    user_config_preserved = $false
    temp_removed = $false
}

try {
    New-Item -ItemType Directory -Path $testRoot, $httpRoot -Force | Out-Null

    Invoke-RobocopyChecked -Source $ComfyUISource -Destination $script:TestWorkspace -ExtraArgs @(
        "/XD", "models", "user", "input", "output", "temp", "custom_nodes", ".git", ".venv", "venv", "__pycache__",
        "/XF", "*.pyc", "*.pyo", "*.log"
    )
    $managerDestination = Join-Path $script:TestWorkspace "custom_nodes\comfyui-manager"
    Invoke-RobocopyChecked -Source $managerSource -Destination $managerDestination -ExtraArgs @(
        "/XD", ".git", "__pycache__", ".venv", "venv",
        "/XF", "*.pyc", "*.pyo", "*.log"
    )

    Invoke-RobocopyChecked -Source $SourceRepo -Destination $sourceCopy -ExtraArgs @(
        "/XD", ".git", ".playwright-cli", "__pycache__", ".venv", "venv", "node_modules", "dist", "build",
        "/XF", "node.zip", "*.pyc", "*.pyo", "*.part", "*.log"
    )
    & git -C $sourceCopy init --initial-branch=main | Out-Null
    & git -C $sourceCopy config user.name "MMFetcher Lifecycle Test"
    & git -C $sourceCopy config user.email "mmfetcher-lifecycle@local.invalid"
    & git -C $sourceCopy add --all
    & git -C $sourceCopy commit -m "lifecycle v1" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to create lifecycle source commit" }
    $v1 = (& git -C $sourceCopy rev-parse HEAD).Trim()

    & git clone --bare $sourceCopy $bareRepo | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to create lifecycle bare repository" }
    & git --git-dir=$bareRepo update-server-info

    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    $listener.Stop()
    $server = Start-Process -FilePath $Python -ArgumentList @(
        "-3.13", "-m", "http.server", $port, "--bind", "127.0.0.1", "--directory", $httpRoot
    ) -WindowStyle Hidden -PassThru
    $repoUrl = "http://127.0.0.1:$port/mmf.git"

    $deadline = (Get-Date).AddSeconds(20)
    do {
        try {
            Invoke-WebRequest -Uri "$repoUrl/info/refs" -UseBasicParsing -TimeoutSec 2 | Out-Null
            $ready = $true
        } catch {
            Start-Sleep -Milliseconds 250
        }
    } until ($ready -or (Get-Date) -ge $deadline)
    if (-not $ready) { throw "Local lifecycle Git server did not start" }

    $privateConfig = Join-Path $script:TestWorkspace "user\__missing_models_fetcher"
    New-Item -ItemType Directory -Path $privateConfig -Force | Out-Null
    $sentinel = Join-Path $privateConfig "lifecycle-sentinel.txt"
    New-Item -ItemType File -Path $sentinel -Force | Out-Null

    Invoke-ComfyNode -Arguments @("install", $repoUrl, "--no-deps", "--exit-on-fail")
    $activePath = Join-Path $script:TestWorkspace "custom_nodes\mmf"
    if (-not (Test-Path -LiteralPath (Join-Path $activePath "pyproject.toml"))) {
        throw "Manager install did not create the expected active node"
    }
    if ((& git -C $activePath rev-parse HEAD).Trim() -ne $v1) {
        throw "Installed commit does not match lifecycle v1"
    }
    $report.install = $true

    Invoke-ComfyNode -Arguments @("disable", "mmf")
    $disabledPath = Join-Path $script:TestWorkspace "custom_nodes\.disabled\mmf"
    if ((Test-Path -LiteralPath $activePath) -or -not (Test-Path -LiteralPath $disabledPath)) {
        throw "Manager disable did not move the node into custom_nodes/.disabled"
    }
    $report.disable = $true

    Invoke-ComfyNode -Arguments @("enable", "mmf")
    if (-not (Test-Path -LiteralPath $activePath) -or (Test-Path -LiteralPath $disabledPath)) {
        throw "Manager enable did not restore the active node"
    }
    $report.enable = $true

    & git -C $sourceCopy commit --allow-empty -m "lifecycle v2" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to create lifecycle v2 commit" }
    $v2 = (& git -C $sourceCopy rev-parse HEAD).Trim()
    & git -C $sourceCopy push $bareRepo main | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to publish lifecycle v2 commit" }
    & git --git-dir=$bareRepo update-server-info

    Invoke-ComfyNode -Arguments @("update", "mmf", "--no-uv-compile")
    if ((& git -C $activePath rev-parse HEAD).Trim() -ne $v2) {
        throw "Manager update did not advance to lifecycle v2"
    }
    $report.update = $true

    Invoke-ComfyNode -Arguments @("uninstall", "mmf")
    if ((Test-Path -LiteralPath $activePath) -or (Test-Path -LiteralPath $disabledPath)) {
        throw "Manager uninstall left the test node installed"
    }
    $report.uninstall = $true
    $report.user_config_preserved = Test-Path -LiteralPath $sentinel
    if (-not $report.user_config_preserved) {
        throw "Manager uninstall removed plugin-private user data"
    }
    $report.ok = $true
}
finally {
    if ($server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id -Force
    }
    if (-not $KeepTemp -and (Test-Path -LiteralPath $testRoot)) {
        $resolved = (Resolve-Path -LiteralPath $testRoot).Path
        if ($resolved.StartsWith($tempBase + "\", [System.StringComparison]::OrdinalIgnoreCase) -and
            (Split-Path -Leaf $resolved).StartsWith("mmf-manager-lifecycle-")) {
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
    }
    $report.temp_removed = -not (Test-Path -LiteralPath $testRoot)
}

$report | ConvertTo-Json -Compress
if (-not $report.ok -or -not $report.temp_removed) {
    exit 1
}
