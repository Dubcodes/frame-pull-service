$root = Split-Path -Parent $PSScriptRoot; Set-Location $root
& .\.venv\Scripts\python.exe -m uvicorn frame_pull_service.main:app --host 127.0.0.1 --port 8094
