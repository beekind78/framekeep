$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path -Parent $PSScriptRoot
$pythonExecutable = Join-Path $projectDirectory '.venv\Scripts\pythonw.exe'
$logDirectory = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.framekeep\logs'
try {
    if (-not (Test-Path -LiteralPath $pythonExecutable)) {
        throw 'Please run Setup Framekeep.cmd once, then open this launcher again.'
    }
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $launchId = [Guid]::NewGuid().ToString('N').Substring(0, 12)
    Start-Process -FilePath $pythonExecutable -ArgumentList @('desktop.py') -WorkingDirectory $projectDirectory -WindowStyle Normal -RedirectStandardOutput (Join-Path $logDirectory ('desktop-' + $launchId + '.log')) -RedirectStandardError (Join-Path $logDirectory ('desktop-' + $launchId + '-error.log'))
} catch {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($_.Exception.Message, 'Framekeep', 'OK', 'Information') | Out-Null
}
