$root = Split-Path -Parent $PSScriptRoot; Set-Location $root
$worker = Start-Process -FilePath '.\.venv\Scripts\python.exe' -ArgumentList '-m frame_pull_service.worker_entry' -PassThru -WindowStyle Hidden
try { & .\.venv\Scripts\python.exe -m uvicorn frame_pull_service.main:app --host 127.0.0.1 --port 8094 } finally { if (-not $worker.HasExited) { Stop-Process -Id $worker.Id } }
