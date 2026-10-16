$projectPath = $PSScriptRoot
$interpreterPath = Join-Path $projectPath '.venv\Scripts\python.exe'
& $interpreterPath -B (Join-Path $projectPath 'preview.py') @args
