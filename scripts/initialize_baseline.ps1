$root = Split-Path -Parent $PSScriptRoot; Set-Location $root
& .\.venv\Scripts\python.exe -c "from frame_pull_service.main import app; from frame_pull_service.services.discovery import discover_recordings; s=app.state.service; session=s.sessions(); print(discover_recordings(session,s.settings)); session.close()"
