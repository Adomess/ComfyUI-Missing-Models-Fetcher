param(
    [Parameter(Mandatory = $true)]
    [string]$ComfyUIRoot,

    [string]$Python = "python",
    [string]$Node = "node"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $ComfyUIRoot)) {
    throw "ComfyUI root does not exist: $ComfyUIRoot"
}

$env:COMFYUI_ROOT = (Resolve-Path -LiteralPath $ComfyUIRoot).Path

& $Python -m py_compile `
    __init__.py `
    missing_models_fetcher\__init__.py `
    missing_models_fetcher\config.py `
    missing_models_fetcher\credential_monitor.py `
    missing_models_fetcher\folders.py `
    missing_models_fetcher\scanner.py `
    missing_models_fetcher\urls.py `
    missing_models_fetcher\downloader.py `
    missing_models_fetcher\routes.py `
    missing_models_fetcher\nodes.py `
    tests\smoke_backend.py `
    scripts\live_network_regression.py `
    scripts\live_download_e2e.py
if ($LASTEXITCODE -ne 0) { throw "Python syntax validation failed with exit code $LASTEXITCODE" }

& $Node --check web\js\missing_models_fetcher.js
if ($LASTEXITCODE -ne 0) { throw "Frontend syntax validation failed with exit code $LASTEXITCODE" }
& $Node --check web\js\mmf_state.mjs
if ($LASTEXITCODE -ne 0) { throw "Frontend state module syntax validation failed with exit code $LASTEXITCODE" }
& $Node tests\frontend_state.mjs
if ($LASTEXITCODE -ne 0) { throw "Frontend state tests failed with exit code $LASTEXITCODE" }
& $Python tests\smoke_backend.py
if ($LASTEXITCODE -ne 0) { throw "Backend smoke tests failed with exit code $LASTEXITCODE" }

$managerTokens = $null
$managerErrors = $null
$null = [System.Management.Automation.Language.Parser]::ParseFile(
    (Resolve-Path -LiteralPath scripts\manager_lifecycle_e2e.ps1),
    [ref]$managerTokens,
    [ref]$managerErrors
)
if ($managerErrors.Count -gt 0) { throw "Manager lifecycle script syntax validation failed" }

Write-Host "Validation completed."
