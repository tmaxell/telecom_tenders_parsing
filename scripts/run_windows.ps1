param(
    [Parameter(Position = 0)]
    [ValidateSet(
        "stats",
        "last",
        "search",
        "collect",
        "collect-bico",
        "collect-bico-all",
        "filter",
        "export-csv",
        "export-excel",
        "export-html",
        "interactive"
    )]
    [string]$Command = "stats",

    [string]$Search = "",
    [string]$Query = "",
    [int]$Count = 30,
    [int]$MaxPages = 5,
    [string]$Product = "",
    [switch]$VerifyKeywords
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host ".venv not found. Running setup first..."
    & (Join-Path $PSScriptRoot "setup_windows.ps1")
}

New-Item -ItemType Directory -Force -Path "data", "logs", "data\exports" | Out-Null

switch ($Command) {
    "stats" {
        & $venvPython -m src.viewer --stats
    }
    "last" {
        & $venvPython -m src.viewer --last $Count
    }
    "search" {
        if (-not $Query) {
            throw "Use -Query `"text`" with the search command."
        }
        & $venvPython -m src.viewer --search $Query
    }
    "collect" {
        & $venvPython -m src.collector --max-pages $MaxPages
    }
    "collect-bico" {
        if (-not $Search) {
            throw "Use -Search `"text`" with the collect-bico command."
        }
        $moduleArgs = @("-m", "src.collector_bico", "--search", $Search, "--max-pages", $MaxPages)
        if ($VerifyKeywords) {
            $moduleArgs += "--verify-keywords"
        }
        & $venvPython @moduleArgs
    }
    "collect-bico-all" {
        $moduleArgs = @("-m", "src.collector_bico", "--all-keywords", "--max-pages", $MaxPages)
        if ($Product) {
            $moduleArgs += @("--product", $Product)
        }
        if ($VerifyKeywords) {
            $moduleArgs += "--verify-keywords"
        }
        & $venvPython @moduleArgs
    }
    "filter" {
        if ($Product) {
            & $venvPython -m src.filter --product $Product --stats
        } else {
            & $venvPython -m src.filter --stats
        }
    }
    "export-csv" {
        & $venvPython -m src.viewer --export csv
    }
    "export-excel" {
        & $venvPython -m src.viewer --export excel
    }
    "export-html" {
        & $venvPython -m src.viewer --export html
    }
    "interactive" {
        & $venvPython -m src.viewer -i
    }
}
