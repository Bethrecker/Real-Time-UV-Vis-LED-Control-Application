$ErrorActionPreference = 'Stop'
$projectPath = $PSScriptRoot
$interpreterPath = Join-Path $projectPath '.venv\Scripts\pythonw.exe'
$applicationPath = Join-Path $projectPath 'gui.py'
$iconPath = Join-Path $projectPath 'assets\pink-led.ico'
foreach ($requiredPath in @($interpreterPath, $applicationPath, $iconPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Required shortcut file is missing: $requiredPath"
    }
}
$desktopPath = [Environment]::GetFolderPath('Desktop')
if (-not (Test-Path -LiteralPath $desktopPath -PathType Container)) {
    throw 'Windows Desktop folder was not found.'
}
$applicationName = 'Real-Time UV' + [char]0x2013 + 'Vis & LED Control'
$shortcutPath = Join-Path $desktopPath ($applicationName + '.lnk')
$launchArguments = '-B "' + $applicationPath + '"'
$shell = New-Object -ComObject WScript.Shell
try {
    $shortcut = $shell.CreateShortcut($shortcutPath)
    if (Test-Path -LiteralPath $shortcutPath) {
        if ($shortcut.TargetPath -ne $interpreterPath -or $shortcut.Arguments -ne $launchArguments) {
            throw 'A shortcut with this name already points to a different program. It has been preserved.'
        }
    }
    $shortcut.TargetPath = $interpreterPath
    $shortcut.Arguments = $launchArguments
    $shortcut.WorkingDirectory = $projectPath
    $shortcut.IconLocation = $iconPath + ',0'
    $shortcut.Description = $applicationName + ' - open the experiment GUI'
    $shortcut.WindowStyle = 1
    $shortcut.Save()
    [pscustomobject]@{
        Shortcut = $shortcutPath
        Target = $shortcut.TargetPath
        Arguments = $shortcut.Arguments
        WorkingDirectory = $shortcut.WorkingDirectory
        Icon = $shortcut.IconLocation
    } | ConvertTo-Json
}
finally {
    if ($null -ne $shortcut) { $null = [Runtime.InteropServices.Marshal]::ReleaseComObject($shortcut) }
    $null = [Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
}
