$root = Split-Path -Parent $PSScriptRoot; Set-Location $root
& .\.venv\Scripts\python.exe -m frame_pull_service.worker_entry
