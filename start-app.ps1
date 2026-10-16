$projectPath = $PSScriptRoot
$interpreterPath = Join-Path $projectPath '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $interpreterPath)) {
    throw 'Project virtual environment is missing. See README.md for setup.'
}
& $interpreterPath -B (Join-Path $projectPath 'gui.py') @args
