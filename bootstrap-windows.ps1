param(
    [string]$Repository = (Get-Location).Path
)

$ErrorActionPreference = 'Stop'

function Find-SupportedPython {
    foreach ($candidate in @('py', 'python3', 'python')) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        $probe = & $command.Source -c 'import platform,sys; print("%d.%d %s" % (*sys.version_info[:2], platform.machine()))' 2>$null
        if ($LASTEXITCODE -eq 0 -and $probe) {
            $parts = $probe.Trim().Split(' ')
            $version = $parts[0].Split('.')
            if (($parts[1] -in @('AMD64', 'x86_64')) -and
                ([int]$version[0] -gt 3 -or ([int]$version[0] -eq 3 -and [int]$version[1] -ge 10))) {
                return $command.Source
            }
        }
    }
    return $null
}

$python = Find-SupportedPython
if (-not $python) {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw 'Python 3.10+ x64 is required. Install it from python.org or install Windows Package Manager, then rerun this script.'
    }
    $answer = Read-Host 'Install Python 3.12 x64 for this Windows user with winget? [y/N]'
    if ($answer -notin @('y', 'Y', 'yes', 'YES')) { throw 'No changes made. Install Python 3.10+ x64 and rerun.' }
    & $winget.Source install --id Python.Python.3.12 --exact --scope user
    if ($LASTEXITCODE -ne 0) { throw 'winget did not install Python successfully.' }
    $python = Find-SupportedPython
    if (-not $python) { throw 'Python was installed but is not yet available on PATH. Open a new PowerShell window and rerun.' }
}

if (-not (Test-Path (Join-Path $Repository 'korelab/__main__.py'))) {
    throw 'Run this bootstrap from a KoreLab checkout or pass -Repository <path>.'
}
$venv = Join-Path $Repository '.venv'
& $python -m venv $venv
if ($LASTEXITCODE -ne 0) { throw 'Could not create the local Python environment.' }
Write-Host "KoreLab Python environment created at $venv. Activate with: .\.venv\Scripts\Activate.ps1"
Write-Host 'The runtime uses Python standard-library dependencies. Use `python -m korelab doctor` to inspect optional providers.'
