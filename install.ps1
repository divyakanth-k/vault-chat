# Bootstrap for Windows. Finds a Python and hands over to install.py, which
# holds all the real logic so the three platforms cannot drift apart.
#
#   powershell -ExecutionPolicy Bypass -File .\install.ps1

$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Definition

foreach ($name in @("py", "python3", "python")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) {
        if ($name -eq "py") {
            & $cmd.Source -3 (Join-Path $dir "install.py") @args
        } else {
            & $cmd.Source (Join-Path $dir "install.py") @args
        }
        exit $LASTEXITCODE
    }
}

Write-Host "Python 3.9 or newer is required but was not found on PATH." -ForegroundColor Red
Write-Host "  winget install Python.Python.3.12"
Write-Host "  (or install from python.org and tick 'Add python.exe to PATH')"
exit 1
