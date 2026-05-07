@echo off
REM Launcher Flask app packing_router (port 5000 default).
REM Pastikan dependency sudah ke-install: pip install -r packing_router\requirements.txt
cd /d "%~dp0\.."
python -m packing_router.web.app
