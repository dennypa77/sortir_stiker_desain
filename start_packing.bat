@echo off
echo ====================================================
echo Menjalankan Auto-Updater Bot Sortir Stiker...
echo ====================================================
py -3.13 updater.py

echo ====================================================
echo Memastikan dependency packing_router terinstal...
echo ====================================================
py -3.13 -m pip install -q -r packing_router\requirements.txt

echo ====================================================
echo Memulai Packing Router (web dashboard)...
echo Buka browser: http://localhost:5000
echo ====================================================
py -3.13 -m packing_router.web.app
pause
