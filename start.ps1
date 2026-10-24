$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Install Docker Desktop from docker.com and enable WSL2, then run this script again.'
}
if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Write-Host 'Created .env. Fill in your SMTP settings locally, save, and close Notepad.'
    Start-Process notepad.exe -ArgumentList '.env' -Wait
}
docker compose up --build -d
if ($LASTEXITCODE -ne 0) { throw 'Docker startup failed. Check that Docker Desktop is running.' }
Write-Host 'Agent started. Daily schedule: 08:00 Europe/Sofia. View status with docker compose logs -f'
