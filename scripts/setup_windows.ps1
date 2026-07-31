param(
    [string]$PythonCommand = "py"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

function Resolve-Python {
    param([string]$Preferred)

    if ($Preferred -eq "py") {
        $candidate = Get-Command py -ErrorAction SilentlyContinue
        if ($candidate) {
            return @{
                Command = "py"
                Args = @("-3")
            }
        }
    }

    foreach ($name in @($Preferred, "python", "python3")) {
        $candidate = Get-Command $name -ErrorAction SilentlyContinue
        if ($candidate) {
            return @{
                Command = $name
                Args = @()
            }
        }
    }

    throw "Python 3.11+ not found. Install Python and enable Add python.exe to PATH."
}

$python = Resolve-Python -Preferred $PythonCommand

Write-Host "Checking Python..."
& $python.Command @($python.Args) --version

if (-not (Test-Path ".venv")) {
    Write-Host "Creating .venv..."
    & $python.Command @($python.Args) -m venv .venv
}

$venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "Virtual environment Python was not created: $venvPython"
}

Write-Host "Installing dependencies..."
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r requirements.txt

New-Item -ItemType Directory -Force -Path "data", "logs", "data\exports" | Out-Null

Write-Host ""
Write-Host "Done. Try:"
Write-Host "  scripts\run_windows.ps1 stats"
Write-Host "  scripts\run_windows.ps1 collect-bico -Search `"IMEI`" -MaxPages 5"
