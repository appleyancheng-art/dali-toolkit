param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$projectDir = $PSScriptRoot
$venvPython = Join-Path $projectDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $Python -m venv (Join-Path $projectDir '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
}
& $venvPython -m pip install -r (Join-Path $projectDir 'requirements.lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'dependency installation failed' }
Push-Location $projectDir
try {
    & $venvPython -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'adapter tests failed' }
    & $venvPython tests\smoke_stdio.py
    if ($LASTEXITCODE -ne 0) { throw 'MCP startup test failed' }
} finally { Pop-Location }
