# Ask the loopback service to shut down, including its background worker.
$projectDirectory = Split-Path -Parent $PSScriptRoot
$pythonExecutable = Join-Path $projectDirectory '.venv\Scripts\pythonw.exe'
if (Test-Path -LiteralPath $pythonExecutable) {
    Start-Process -FilePath $pythonExecutable -ArgumentList @('desktop.py', '--stop') -WorkingDirectory $projectDirectory -WindowStyle Hidden
}
