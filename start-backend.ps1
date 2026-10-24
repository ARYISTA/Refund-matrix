# Start Postgres DB container
docker compose up -d db

# Start Backend in virtualenv
Write-Host "Starting Backend API on http://localhost:8000..." -ForegroundColor Green
Set-Location -Path "$PSScriptRoot\backend"
& ".\.venv\Scripts\uvicorn.exe" app.api:app --reload --port 8000
