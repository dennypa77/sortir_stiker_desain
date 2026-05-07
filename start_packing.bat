@echo off
echo ====================================================
echo Menjalankan Auto-Updater Bot Sortir Stiker...
echo ====================================================
python updater.py

echo ====================================================
echo Memastikan dependency packing_router terinstal...
echo ====================================================
pip install -q -r packing_router\requirements.txt

echo ====================================================
echo Memulai Packing Router (web dashboard)...
echo Buka browser: http://localhost:5000
echo ====================================================
python -m packing_router.web.app
pause
