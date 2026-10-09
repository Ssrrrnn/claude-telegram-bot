# Start the Morrow GitHub Issues receiver on Windows.
# Never executes Harness; never modifies Git, Windows ACLs or Railway.
param(
    [ValidateSet("backend", "ios")]
    [string] $Target = "backend",
    [string] $Workspace = $PSScriptRoot,
    [ValidateRange(30, 3600)]
    [int] $Interval = 60
)

$ErrorActionPreference = "Stop"
$scriptPath = Join-Path $PSScriptRoot "harness_bridge.py"
$parent = Split-Path $PSScriptRoot -Parent
$pythonPath = Join-Path $parent ".venvs\claude-telegram-bot\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "The existing project Python virtual environment was not found; inspect the path before continuing."
}
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw "harness_bridge.py is missing. Checkout the bridge development branch first."
}

if (-not $env:MORROW_HARNESS_TOKEN -and -not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Host "GitHub private Issues need a repository-scoped fine-grained token."
    Write-Host "Token permission: Issues (Read), or Issues (Read/Write) to post reviewed reports."
    $secure = Read-Host "GitHub token (input hidden)" -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $env:MORROW_HARNESS_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}
Write-Host "Starting the RECEIVE-ONLY Morrow Harness task watcher. Ctrl+C stops it."
Write-Host "No Harness execution, git push, PR merge, or Railway deploy is performed."
& $pythonPath $scriptPath --workspace $Workspace --target $Target watch --interval $Interval
exit $LASTEXITCODE
