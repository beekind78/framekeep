$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path -Parent $PSScriptRoot
$pythonExecutable = Join-Path $projectDirectory '.venv\Scripts\pythonw.exe'
$desktopLauncher = Join-Path $projectDirectory 'desktop.py'
$iconPath = Join-Path $projectDirectory 'framekeep\static\framekeep.ico'
if (-not (Test-Path -LiteralPath $pythonExecutable) -or -not (Test-Path -LiteralPath $iconPath)) {
    throw 'Run Setup Framekeep.cmd before creating the Framekeep shortcuts.'
}
$shortcutShell = New-Object -ComObject WScript.Shell
$locations = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))
foreach ($shortcutDirectory in $locations) {
    if ([string]::IsNullOrWhiteSpace($shortcutDirectory)) { continue }
    New-Item -ItemType Directory -Path $shortcutDirectory -Force | Out-Null
    $shortcutPath = Join-Path $shortcutDirectory 'Framekeep.lnk'
    if (Test-Path -LiteralPath $shortcutPath) {
        $previousShortcut = $shortcutShell.CreateShortcut($shortcutPath)
        if ($previousShortcut.WorkingDirectory -ne $projectDirectory) {
            $shortcutPath = Join-Path $shortcutDirectory 'Framekeep Photo Organizer.lnk'
            if (Test-Path -LiteralPath $shortcutPath) {
                $previousShortcut = $shortcutShell.CreateShortcut($shortcutPath)
                if ($previousShortcut.WorkingDirectory -ne $projectDirectory) {
                    throw 'A different app already uses the Framekeep shortcut names.'
                }
            }
        }
    }
    $shortcut = $shortcutShell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $pythonExecutable
    $shortcut.Arguments = '"' + $desktopLauncher + '"'
    $shortcut.WorkingDirectory = $projectDirectory
    $shortcut.IconLocation = $iconPath + ',0'
    $shortcut.Description = 'Framekeep - Your memories, back in order. Local and private.'
    $shortcut.WindowStyle = 1
    $shortcut.Save()
    Write-Output $shortcutPath
}
